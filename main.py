import os
import re
import uuid
from typing import List
import asyncio

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain
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
    
    def needs_rendering(self, text: str, min_complexity_score: int = 2) -> bool:
        """
        判断文本是否需要渲染为图片
        min_complexity_score: 复杂度阈值，达到此分数则转换为图片
        """
        if not text.strip():
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
        self.detector = MarkdownComplexityDetector()
        
        # 配置选项
        self.auto_detect = True  # 开启自动检测
        self.min_complexity_score = 2  # 复杂度阈值
        self.always_convert_codes = True  # 始终转换代码块
        self.respect_md_tags = True  # 尊重显式的<md>标签

    async def initialize(self):
        """初始化插件"""
        try:
            os.makedirs(self.IMAGE_CACHE_DIR, exist_ok=True)
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

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        """向 LLM 说明图片渲染功能的使用方式"""
        if self.auto_detect:
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
        if self.respect_md_tags:
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
            
            # 处理普通文本（自动检测）
            elif self.auto_detect and self.detector.needs_rendering(part, self.min_complexity_score):
                logger.info("检测到复杂Markdown格式，自动转换为图片")
                image_component = await self._convert_markdown_to_image(part)
                if image_component:
                    components.append(image_component)
                else:
                    components.append(Plain(part))
            
            # 简单文本直接发送
            else:
                components.append(Plain(part))
                
        return components

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


# 测试用例
async def test_detector():
    """测试复杂度检测器"""
    detector = MarkdownComplexityDetector()
    
    test_cases = [
        # 简单文本（不应转换）
        "这是一段简单文本，**粗体**和*斜体*都能正常显示。",
        
        # 代码块（应该转换）
        """这是一个代码示例：
```python
def hello():
    print("Hello World")
    return True
```""",
        
        # 数学公式（应该转换）
        """数学公式示例：
行内公式：$E = mc^2$
独立公式：
$$\int_{-\infty}^{\infty} e^{-x^2} dx = \sqrt{\pi}$$""",
        
        # 表格（应该转换）
        """| 姓名 | 年龄 | 职业 |
|------|------|------|
| 张三 | 25   | 工程师 |
| 李四 | 30   | 设计师 |""",
        
        # 混合复杂内容（应该转换）
        """# 标题
这是一个包含多种复杂元素的示例：

1. 代码块
```javascript
console.log("Hello");
```

2. 数学公式
$a^2 + b^2 = c^2$

3. 表格
| 项目 | 值 |
|------|----|
| A    | 1  |"""
    ]
    
    for i, case in enumerate(test_cases):
        needs = detector.needs_rendering(case)
        print(f"测试用例 {i+1}: {'需要转换' if needs else '直接发送'}")
        print("内容预览:", case[:100] + "..." if len(case) > 100 else case)
        print("-" * 50)


if __name__ == "__main__":
    # 运行测试
    asyncio.run(test_detector())
