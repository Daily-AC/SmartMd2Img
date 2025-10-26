import os
import re
import uuid
import json
from typing import List, Dict, Any
import asyncio

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain, File
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_tools import StarTools

import mistune
from playwright.async_api import async_playwright


class MarkdownComplexityDetector:
    """Markdown复杂度检测器，用于判断是否需要转换为图片"""
    
    def __init__(self):
        # 定义需要图片渲染的复杂模式
        self.complex_patterns = {
            'code_block': re.compile(r'```[\s\S]*?```', re.MULTILINE),  # 代码块
            'table': re.compile(r'\|.*\|.*\n\|.*---.*\|.*\n(\|.*\|.*\n)*', re.MULTILINE),  # 表格
            'math_inline': re.compile(r'\$[^$]+\$'),  # 行内数学公式
            'math_block': re.compile(r'\$\$[\s\S]*?\$\$', re.MULTILINE),  # 块级数学公式
            'complex_list': re.compile(r'^(?:\s*[-*+]|\s*\d+\.)\s+.*$(?:\n^(?:\s{4,}[-*+]|\s{4,}\d+\.)\s+.*$)+', re.MULTILINE),  # 复杂嵌套列表
            'blockquote': re.compile(r'^>+.*$(?:\n^>+.*$)*', re.MULTILINE),  # 引用块
            'multiple_headings': re.compile(r'^#{1,6}\s+.+$(?:\n^#{1,6}\s+.+$){1,}', re.MULTILINE),  # 多个标题
        }
        
        # 定义应该保持为文本的模式（不转换为图片）
        self.keep_as_text_patterns = {
            'simple_links': re.compile(r'\[.*?\]\(.*?\)'),  # 简单链接 [文字](URL)
            'url_links': re.compile(r'https?://[^\s]+'),  # 纯URL链接
        }
    
    def needs_rendering(self, text: str, min_complexity_score: int = 2) -> bool:
        """
        判断文本是否需要渲染为图片
        min_complexity_score: 复杂度阈值，达到此分数则转换为图片
        """
        if not text.strip():
            return False
            
        # 如果只有链接，直接返回不需要转换
        if self._only_contains_links(text):
            return False
            
        complexity_score = 0
        
        # 检测复杂模式
        for pattern_name, pattern in self.complex_patterns.items():
            matches = pattern.findall(text)
            if matches:
                if pattern_name == 'code_block':
                    complexity_score += len(matches) * 2  # 代码块权重较高
                elif pattern_name in ['math_block', 'table']:
                    complexity_score += len(matches) * 3  # 数学公式和表格权重最高
                else:
                    complexity_score += len(matches)
        
        # 如果包含多个复杂元素，直接需要渲染
        if complexity_score >= min_complexity_score:
            return True
            
        # 检测文本长度（过长的纯文本在QQ中显示效果也不好）
        lines = text.split('\n')
        if len(lines) > 15:  # 超过15行考虑渲染
            return True
            
        # 检测行长度（避免过长的行在移动端显示问题）
        long_lines = [line for line in lines if len(line.strip()) > 80]
        if len(long_lines) > 3:  # 多行超过80字符
            return True
            
        return False
    
    def _only_contains_links(self, text: str) -> bool:
        """
        检查文本是否只包含链接（或主要是链接）
        如果是，则不应该转换为图片
        """
        text = text.strip()
        if not text:
            return False
            
        # 检查是否包含链接
        has_links = False
        for pattern_name, pattern in self.keep_as_text_patterns.items():
            if pattern.search(text):
                has_links = True
                break
        
        if not has_links:
            return False
            
        # 如果文本主要是链接，则保持为文本
        # 计算链接部分占整个文本的比例
        total_length = len(text)
        link_length = 0
        
        for pattern_name, pattern in self.keep_as_text_patterns.items():
            for match in pattern.finditer(text):
                link_length += len(match.group(0))
        
        # 如果链接部分超过文本的60%，或者文本很短且包含链接
        link_ratio = link_length / total_length if total_length > 0 else 0
        if link_ratio > 0.6 or (total_length < 200 and has_links):
            return True
            
        return False
    
    def extract_code_blocks(self, text: str) -> List[Dict[str, Any]]:
        """提取代码块"""
        code_blocks = []
        # 修复正则表达式，正确处理代码块
        pattern = re.compile(r'```(\w+)?\n?(.*?)\n?```', re.DOTALL)
        
        for match in pattern.finditer(text):
            language = match.group(1) or 'text'
            code_content = match.group(2).strip()
            code_blocks.append({
                'language': language,
                'content': code_content,
                'full_match': match.group(0),  # 保存完整匹配，用于后续替换
                'start': match.start(),
                'end': match.end()
            })
        
        return code_blocks
    
    def extract_math_blocks(self, text: str) -> List[Dict[str, Any]]:
        """提取数学公式块"""
        math_blocks = []
        
        # 提取行内数学公式
        inline_pattern = re.compile(r'\$([^$]+)\$')
        for match in inline_pattern.finditer(text):
            math_content = match.group(1).strip()
            math_blocks.append({
                'type': 'inline',
                'content': math_content,
                'full_match': match.group(0),
                'start': match.start(),
                'end': match.end()
            })
        
        # 提取块级数学公式
        block_pattern = re.compile(r'\$\$(.*?)\$\$', re.DOTALL)
        for match in block_pattern.finditer(text):
            math_content = match.group(1).strip()
            math_blocks.append({
                'type': 'block',
                'content': math_content,
                'full_match': match.group(0),
                'start': match.start(),
                'end': match.end()
            })
        
        return math_blocks


async def markdown_to_image_playwright(
    md_text: str,
    output_image_path: str,
    scale: int = 2,
    width: int = 600
):
    """
    使用 Playwright 将包含 LaTeX 的 Markdown 转换为图片。
    """
    width_style = ""
    if width:
        width_style = f"width: {width}px; box-sizing: border-box;"

    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Markdown Render</title>
        <style>
            body {{
                {width_style}
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
                padding: 25px;
                display: inline-block;
                font-size: 16px;
                -webkit-font-smoothing: antialiased;
                -moz-osx-font-smoothing: grayscale;
                text-rendering: optimizeLegibility;
                background-color: #ffffff;
                color: #24292e;
                line-height: 1.5;
            }}
            pre {{
                background-color: #f6f8fa;
                border-radius: 6px;
                padding: 16px;
                overflow: auto;
                font-size: 85%;
                line-height: 1.45;
            }}
            code {{
                font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
                background-color: rgba(175, 184, 193, 0.2);
                border-radius: 3px;
                padding: 0.2em 0.4em;
                font-size: 85%;
            }}
            pre code {{
                background: none;
                padding: 0;
            }}
            table {{
                border-collapse: collapse;
                width: 100%;
                margin: 1em 0;
            }}
            th, td {{
                border: 1px solid #dfe2e5;
                padding: 6px 13px;
                text-align: left;
            }}
            th {{
                background-color: #f6f8fa;
                font-weight: 600;
            }}
            blockquote {{
                border-left: 4px solid #dfe2e5;
                padding-left: 1em;
                margin-left: 0;
                color: #6a737d;
            }}
            img {{
                max-width: 100%;
            }}
        </style>
        <script type="text/javascript" async
            src="https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.7/MathJax.js?config=TeX-MML-AM_CHTML">
        </script>
        <script type="text/x-mathjax-config">
            MathJax.Hub.Config({{
                tex2jax: {{
                    inlineMath: [['$','$']],
                    displayMath: [['$$','$$']],
                }},
                "HTML-CSS": {{
                    scale: 100,
                    linebreaks: {{ automatic: true }}
                }},
                SVG: {{ linebreaks: {{ automatic: true }} }}
            }});
        </script>
    </head>
    <body>
        {content}
    </body>
    </html>
    """

    html_content = mistune.html(md_text)
    full_html = html_template.format(content=html_content, width_style=width_style)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(device_scale_factor=scale)
        page = await context.new_page()

        await page.set_content(full_html, wait_until="networkidle")

        try:
            await page.evaluate("MathJax.Hub.Queue(['Typeset', MathJax.Hub])")
            await page.wait_for_function("typeof MathJax.Hub.Queue.running === 'undefined' || MathJax.Hub.Queue.running === 0")
        except Exception as e:
            print(f"等待 MathJax 时出错: {e}")

        element_handle = await page.query_selector('body')
        if not element_handle:
            raise Exception("无法找到 <body> 元素进行截图。")

        await element_handle.screenshot(path=output_image_path)
        await browser.close()
        logger.info(f"Markdown 图片已生成: {output_image_path}")


@register(
    "SmartMd2Img",
    "Daily-AC",
    "智能Markdown转图片插件，自动检测复杂格式并转换为图片",
    "1.0.0",
)
class SmartMarkdownConverterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.DATA_DIR = os.path.normpath(StarTools.get_data_dir())
        self.IMAGE_CACHE_DIR = os.path.join(self.DATA_DIR, "md2img_cache")
        self.FILE_CACHE_DIR = os.path.join(self.DATA_DIR, "file_cache")
        self.detector = MarkdownComplexityDetector()
        
        # 加载配置
        self.config = self._load_config()
        
    def _load_config(self) -> Dict[str, Any]:
        """加载配置"""
        default_config = {
            "auto_detect": {
                "description": "启用自动检测复杂Markdown格式并转换为图片",
                "type": "bool",
                "default": True,
                "hint": "关闭后只处理显式<md>标签"
            },
            "min_complexity_score": {
                "description": "自动转换的复杂度阈值",
                "type": "int",
                "default": 2,
                "hint": "数值越高，越不容易触发自动转换"
            },
            "respect_md_tags": {
                "description": "尊重显式的<md>标签",
                "type": "bool",
                "default": True,
                "hint": "启用后<md>内容始终转换为图片"
            },
            "separate_code_blocks": {
                "description": "将代码块从文本中分离处理",
                "type": "bool",
                "default": True,
                "hint": "启用后，代码块会单独处理，文本部分保持原样"
            },
            "separate_math_blocks": {
                "description": "将数学公式从文本中分离处理",
                "type": "bool", 
                "default": False,
                "hint": "启用后，数学公式会单独处理"
            },
            "code_handling_settings": {
                "description": "代码块处理设置",
                "type": "object",
                "items": {
                    "render_code_as_image": {
                        "description": "将代码块渲染为图片",
                        "type": "bool",
                        "default": True,
                        "hint": "启用后代码块会转换为图片"
                    },
                    "send_code_as_file": {
                        "description": "将长代码发送为文件",
                        "type": "bool",
                        "default": False,
                        "hint": "启用后长代码会作为文件发送"
                    },
                    "code_file_threshold": {
                        "description": "代码文件转换阈值（行数）",
                        "type": "int",
                        "default": 10,
                        "hint": "代码超过此行数时，会发送为文件"
                    }
                }
            },
            "math_handling_settings": {
                "description": "数学公式处理设置",
                "type": "object",
                "items": {
                    "render_math_as_image": {
                        "description": "将数学公式渲染为图片",
                        "type": "bool",
                        "default": True,
                        "hint": "启用后数学公式会转换为图片"
                    }
                }
            },
            "supported_code_languages": {
                "description": "支持发送为文件的代码语言列表",
                "type": "list",
                "default": [
                    "python", "javascript", "java", "cpp", "c", 
                    "html", "css", "sql", "bash", "shell",
                    "php", "ruby", "go", "rust", "typescript",
                    "json", "xml", "yaml", "markdown"
                ],
                "hint": "这些语言的代码可以被发送为文件"
            }
        }
        return default_config

    async def initialize(self):
        """初始化插件"""
        try:
            os.makedirs(self.IMAGE_CACHE_DIR, exist_ok=True)
            os.makedirs(self.FILE_CACHE_DIR, exist_ok=True)
            logger.info("正在检查并安装 Playwright 浏览器依赖...")
            
            async def run_playwright_command(command: list, description: str):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    logger.error(f"自动安装 Playwright {description} 失败，返回码: {process.returncode}")
                    if stderr:
                        logger.error(f"错误输出: \n{stderr.decode('utf-8', errors='ignore')}")
                    return False
                else:
                    output = stdout.decode('utf-8', errors='ignore')
                    if "up to date" not in output:
                        logger.info(f"Playwright {description} 安装/更新完成。")
                    else:
                        logger.info(f"Playwright {description} 已是最新。")
                    return True

            # 安装浏览器和依赖
            import sys
            install_browser_cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
            await run_playwright_command(install_browser_cmd, "Chromium 浏览器")
            
            install_deps_cmd = [sys.executable, "-m", "playwright", "install-deps"]
            await run_playwright_command(install_deps_cmd, "系统依赖")

            logger.info("智能 Markdown 转图片插件已初始化")

        except Exception as e:
            logger.error(f"插件初始化过程中发生错误: {e}")

    async def terminate(self):
        """插件停用时调用"""
        logger.info("智能 Markdown 转图片插件已停止")

    def get_config_json(self) -> str:
        """返回配置JSON"""
        return json.dumps(self.config, ensure_ascii=False, indent=2)

    def _get_config_value(self, key: str):
        """获取配置值"""
        # 这里应该从实际的配置存储中获取，这里简化处理返回默认值
        return self.config[key]["default"]
    
    def _get_object_config_value(self, object_key: str, item_key: str):
        """获取对象配置中的值"""
        object_config = self._get_config_value(object_key)
        if isinstance(object_config, dict) and item_key in object_config:
            return object_config[item_key]["default"]
        return None

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        """向 LLM 说明图片渲染功能的使用方式"""
        auto_detect = self._get_config_value("auto_detect")
        
        if auto_detect:
            instruction_prompt = """
你可以自由地使用Markdown格式来编写回复。系统会自动检测复杂的格式（如代码块、表格、数学公式等）并将其转换为图片，以确保在QQ中能够完美显示。

如果你希望强制将某段内容转换为图片，可以继续使用 <md> 和 </md> 标签包裹内容。

简单格式（如粗体、斜体、简单列表等）会保持为文本直接发送。
"""
        else:
            instruction_prompt = """
当你需要发送包含复杂格式（如代码块、表格、数学公式等）的内容时，请使用 <md> 和 </md> 标签包裹需要转换为图片的Markdown内容。

例如：
<md>
# 复杂内容标题
```python
print("Hello World")
```
</md>
"""
        req.system_prompt += f"\n\n{instruction_prompt}"

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """保存原始响应"""
        event.set_extra("raw_llm_completion_text", resp.completion_text)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在最终消息链生成阶段，智能处理Markdown内容"""
        result = event.get_result()
        chain = result.chain
        new_chain = []
        
        for item in chain:
            if isinstance(item, Plain):
                components = await self._smart_process_markdown(item.text)
                new_chain.extend(components)
            else:
                new_chain.append(item)
                
        result.chain = new_chain

    async def _smart_process_markdown(self, text: str) -> List:
        """
        智能处理Markdown文本，自动判断是否需要转换为图片
        """
        components = []

        # 首先处理显式的<md>标签（如果启用）
        respect_md_tags = self._get_config_value("respect_md_tags")
        if respect_md_tags:
            pattern = r"(<md>.*?</md>)"
            parts = re.split(pattern, text, flags=re.DOTALL)
        else:
            parts = [text]

        for part in parts:
            part = part.strip()
            if not part:
                continue
                
            # 处理显式的<md>标签
            if part.startswith("<md>") and part.endswith("</md>"):
                md_content = part[4:-5].strip()
                if md_content:
                    image_component = await self._convert_markdown_to_image(md_content)
                    if image_component:
                        components.append(image_component)
                    else:
                        components.append(Plain(f"--- Markdown 渲染失败 ---\n{md_content}"))
            
            # 处理普通文本
            else:
                processed_components = await self._process_text_with_blocks(part)
                components.extend(processed_components)
                
        return components

    async def _process_text_with_blocks(self, text: str) -> List:
        """处理文本，分离代码块和数学公式"""
        components = []
        
        # 检查是否启用代码块分离
        separate_code = self._get_config_value("separate_code_blocks")
        separate_math = self._get_config_value("separate_math_blocks")
        
        # 修复：即使不分离代码块和数学公式，也要正确处理文本
        if not separate_code and not separate_math:
            # 不分离块，直接处理整个文本
            return await self._process_plain_text(text)
        
        # 提取代码块和数学公式
        code_blocks = self.detector.extract_code_blocks(text) if separate_code else []
        math_blocks = self.detector.extract_math_blocks(text) if separate_math else []
        
        # 将所有块合并并按位置排序
        all_blocks = []
        for block in code_blocks:
            block['block_type'] = 'code'
            all_blocks.append(block)
        for block in math_blocks:
            block['block_type'] = 'math'
            all_blocks.append(block)
        
        # 按位置排序
        all_blocks.sort(key=lambda x: x['start'])
        
        if all_blocks:
            # 分割文本和块
            last_pos = 0
            for block in all_blocks:
                # 添加块前的文本
                if block['start'] > last_pos:
                    text_before = text[last_pos:block['start']]
                    if text_before.strip():
                        text_components = await self._process_plain_text(text_before)
                        components.extend(text_components)
                
                # 处理块
                if block['block_type'] == 'code':
                    code_components = await self._process_code_block(block)
                    components.extend(code_components)
                else:  # math
                    math_components = await self._process_math_block(block)
                    components.extend(math_components)
                
                last_pos = block['end']
            
            # 添加最后剩余的文本
            if last_pos < len(text):
                text_after = text[last_pos:]
                if text_after.strip():
                    text_components = await self._process_plain_text(text_after)
                    components.extend(text_components)
        else:
            # 没有特殊块，直接处理文本
            text_components = await self._process_plain_text(text)
            components.extend(text_components)
            
        return components

    async def _process_plain_text(self, text: str) -> List:
        """处理纯文本（自动检测是否需要渲染）"""
        components = []
        
        # 检查是否主要是链接内容
        if self.detector._only_contains_links(text):
            logger.info("检测到链接内容，保持为文本直接发送")
            components.append(Plain(text))
        
        # 处理自动检测
        elif self._get_config_value("auto_detect") and self.detector.needs_rendering(text, self._get_config_value("min_complexity_score")):
            logger.info("检测到复杂Markdown格式，自动转换为图片")
            image_component = await self._convert_markdown_to_image(text)
            if image_component:
                components.append(image_component)
            else:
                components.append(Plain(text))
        
        # 简单文本直接发送
        else:
            components.append(Plain(text))
            
        return components

    async def _process_code_block(self, code_block: Dict) -> List:
        """处理代码块"""
        render_code = self._get_object_config_value("code_handling_settings", "render_code_as_image")
        send_file = self._get_object_config_value("code_handling_settings", "send_code_as_file")
        file_threshold = self._get_object_config_value("code_handling_settings", "code_file_threshold")
        
        language = code_block['language']
        content = code_block['content']
        supported_languages = self._get_config_value("supported_code_languages")
        
        components = []
        
        # 计算代码行数
        line_count = len(content.split('\n'))
        logger.info(f"代码块处理: 语言={language}, 行数={line_count}, 阈值={file_threshold}")
        
        # 检查是否应该发送为文件
        should_send_file = (send_file and 
                           language.lower() in [lang.lower() for lang in supported_languages] and 
                           line_count > file_threshold)
        
        logger.info(f"是否发送文件: {should_send_file}")
        
        if should_send_file:
            # 发送为文件
            file_component = await self._create_code_file(content, language)
            if file_component:
                components.append(file_component)
                # 添加简短的代码预览
                preview_lines = content.split('\n')[:5]  # 显示前5行作为预览
                preview = '\n'.join(preview_lines)
                if line_count > 5:
                    preview += f"\n... (共{line_count}行，完整代码已发送为文件)"
                components.append(Plain(f"```{language}\n{preview}\n```"))
            else:
                # 文件创建失败，回退到渲染或直接发送
                if render_code:
                    md_content = f"```{language}\n{content}\n```"
                    image_component = await self._convert_markdown_to_image(md_content)
                    if image_component:
                        components.append(image_component)
                    else:
                        components.append(Plain(f"```{language}\n{content}\n```"))
                else:
                    components.append(Plain(f"```{language}\n{content}\n```"))
        elif render_code:
            # 渲染为图片
            md_content = f"```{language}\n{content}\n```"
            image_component = await self._convert_markdown_to_image(md_content)
            if image_component:
                components.append(image_component)
            else:
                components.append(Plain(f"```{language}\n{content}\n```"))
        else:
            # 直接发送代码文本
            components.append(Plain(f"```{language}\n{content}\n```"))
        
        return components

    async def _process_math_block(self, math_block: Dict) -> List:
        """处理数学公式块"""
        render_math = self._get_object_config_value("math_handling_settings", "render_math_as_image")
        math_type = math_block['type']
        content = math_block['content']
        
        components = []
        
        if render_math:
            # 渲染为图片
            if math_type == 'inline':
                md_content = f"${content}$"
            else:
                md_content = f"$$\n{content}\n$$"
                
            image_component = await self._convert_markdown_to_image(md_content)
            if image_component:
                components.append(image_component)
            else:
                components.append(Plain(md_content))
        else:
            # 保持原样
            if math_type == 'inline':
                components.append(Plain(f"${content}$"))
            else:
                components.append(Plain(f"$$\n{content}\n$$"))
        
        return components

    async def _create_code_file(self, code_content: str, language: str) -> File:
        """创建代码文件"""
        # 确定文件扩展名
        ext_map = {
            'python': 'py', 'javascript': 'js', 'java': 'java', 'cpp': 'cpp',
            'c': 'c', 'html': 'html', 'css': 'css', 'sql': 'sql',
            'bash': 'sh', 'shell': 'sh', 'php': 'php', 'ruby': 'rb',
            'go': 'go', 'rust': 'rs', 'typescript': 'ts', 'json': 'json',
            'xml': 'xml', 'yaml': 'yml', 'markdown': 'md', 'text': 'txt'
        }
        
        ext = ext_map.get(language.lower(), 'txt')
        filename = f"code_{uuid.uuid4().hex[:8]}.{ext}"
        filepath = os.path.join(self.FILE_CACHE_DIR, filename)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(code_content)
            
            # 使用正确的File组件创建方式
            return File(file=filepath, name=filename)
        except Exception as e:
            logger.error(f"创建代码文件失败: {e}")
            return None

    async def _convert_markdown_to_image(self, md_content: str) -> Image:
        """将Markdown内容转换为图片"""
        image_filename = f"{uuid.uuid4()}.png"
        output_path = os.path.join(self.IMAGE_CACHE_DIR, image_filename)
        
        try:
            await markdown_to_image_playwright(
                md_text=md_content,
                output_image_path=output_path,
                scale=2,
                width=600
            )
            
            if os.path.exists(output_path):
                return Image.fromFileSystem(output_path)
            else:
                logger.error(f"Markdown 图片生成失败: {output_path}")
                return None
                
        except Exception as e:
            logger.error(f"Markdown 转换异常: {e}")
            return None


# 配置选项JSON
CONFIG_JSON = {
    "auto_detect": {
        "description": "启用自动检测复杂Markdown格式并转换为图片",
        "type": "bool",
        "default": True,
        "hint": "关闭后只处理显式<md>标签"
    },
    "min_complexity_score": {
        "description": "自动转换的复杂度阈值",
        "type": "int", 
        "default": 2,
        "hint": "数值越高，越不容易触发自动转换"
    },
    "respect_md_tags": {
        "description": "尊重显式的<md>标签",
        "type": "bool",
        "default": True,
        "hint": "启用后<md>内容始终转换为图片"
    },
    "separate_code_blocks": {
        "description": "将代码块从文本中分离处理",
        "type": "bool",
        "default": True,
        "hint": "启用后，代码块会单独处理，文本部分保持原样"
    },
    "separate_math_blocks": {
        "description": "将数学公式从文本中分离处理",
        "type": "bool",
        "default": False, 
        "hint": "启用后，数学公式会单独处理"
    },
    "code_handling_settings": {
        "description": "代码块处理设置",
        "type": "object",
        "items": {
            "render_code_as_image": {
                "description": "将代码块渲染为图片",
                "type": "bool",
                "default": True,
                "hint": "启用后代码块会转换为图片"
            },
            "send_code_as_file": {
                "description": "将长代码发送为文件",
                "type": "bool",
                "default": False,
                "hint": "启用后长代码会作为文件发送"
            },
            "code_file_threshold": {
                "description": "代码文件转换阈值（行数）",
                "type": "int",
                "default": 10,
                "hint": "代码超过此行数时，会发送为文件"
            }
        }
    },
    "math_handling_settings": {
        "description": "数学公式处理设置",
        "type": "object",
        "items": {
            "render_math_as_image": {
                "description": "将数学公式渲染为图片",
                "type": "bool",
                "default": True,
                "hint": "启用后数学公式会转换为图片"
            }
        }
    },
    "supported_code_languages": {
        "description": "支持发送为文件的代码语言列表",
        "type": "list",
        "default": [
            "python", "javascript", "java", "cpp", "c", 
            "html", "css", "sql", "bash", "shell",
            "php", "ruby", "go", "rust", "typescript",
            "json", "xml", "yaml", "markdown"
        ],
        "hint": "这些语言的代码可以被发送为文件"
    }
}

if __name__ == "__main__":
    # 输出配置JSON
    print("配置选项JSON:")
    print(json.dumps(CONFIG_JSON, ensure_ascii=False, indent=2))
