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

# 字体大小配置 - 方便测试调整
code_font_size = 13
line_height = 1.5
from bs4 import BeautifulSoup
def process_code_blocks_in_html(html_content: str) -> str:
    """在Python中预处理代码块，生成带行号的HTML"""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # 找到所有代码块
    for pre in soup.find_all('pre'):
        code = pre.find('code')
        if not code:
            continue
        
        # 获取语言和文本
        lang = 'text'
        if code.get('class'):
            match = re.search(r'language-(\w+)', ' '.join(code.get('class')))
            if match:
                lang = match.group(1)
        
        # 获取原始文本内容
        text_content = code.get_text()
        lines = text_content.split('\n')
        
        # 移除末尾空行
        while lines and lines[-1] == '':
            lines.pop()
        
        if not lines:
            lines = ['']
        
        line_count = len(lines)
        
        # 构建新的HTML结构
        container = soup.new_tag('div', attrs={'class': 'code-container'})
        
        # 标题栏
        header = soup.new_tag('div', attrs={'class': 'code-header'})
        header.string = lang
        container.append(header)
        
        # 内容区域
        content = soup.new_tag('div', attrs={'class': 'code-content'})
        
        # 行号
        line_nums = soup.new_tag('div', attrs={'class': 'line-numbers'})
        for i in range(1, line_count + 1):
            line_num = soup.new_tag('div', attrs={'class': 'line-number'})
            line_num.string = str(i)
            line_nums.append(line_num)
        content.append(line_nums)
        
        # 代码包装
        code_wrapper = soup.new_tag('div', attrs={'class': 'code-wrapper'})
        new_pre = soup.new_tag('pre')
        new_code = soup.new_tag('code', attrs={'class': code.get('class', [])})
        new_code.string = text_content  # 保留原始文本
        new_pre.append(new_code)
        code_wrapper.append(new_pre)
        content.append(code_wrapper)
        
        container.append(content)
        
        # 替换原来的pre标签
        pre.replace_with(container)
    
    return str(soup)


# 在 safe_format 函数中添加调试
def safe_format(template, **kwargs):
    """安全的字符串格式化，忽略不存在的键"""
    import string
    from string import Formatter
    
    formatter = Formatter()
    valid_keys = set()
    for literal_text, field_name, format_spec, conversion in formatter.parse(template):
        if field_name is not None:
            valid_keys.add(field_name)
    
    # 调试信息
    missing_keys = valid_keys - set(kwargs.keys())
    if missing_keys:
        logger.warning(f"模板中缺少以下键: {missing_keys}")
    
    safe_kwargs = {k: v for k, v in kwargs.items() if k in valid_keys}
    return template.format(**safe_kwargs)

async def markdown_to_image_playwright(
    md_text: str,
    output_image_path: str,
    scale: int = 2,
    width: int = 600
):
    """
    使用 Playwright 将 Markdown 转换为图片（修复格式化版本）
    """
    def safe_format(template, **kwargs):
        """安全的字符串格式化，忽略不存在的键"""
        import string
        from string import Formatter
        
        formatter = Formatter()
        valid_keys = set()
        for literal_text, field_name, format_spec, conversion in formatter.parse(template):
            if field_name is not None:
                valid_keys.add(field_name)
        
        safe_kwargs = {k: v for k, v in kwargs.items() if k in valid_keys}
        return template.format(**safe_kwargs)
    
    def escape_curly_braces(text):
        """转义文本中的花括号，防止在格式化时被解析"""
        return text.replace('{', '{{').replace('}', '}}')

    width_style = f"width: {width}px; box-sizing: border-box;" if width else ""
    
    try:
        html_template = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Markdown Render</title>
            <style>
                * {{
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }}
                
                body {{
                    {width_style}
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
                    padding: 20px;
                    font-size: 16px;
                    line-height: 1.6;
                    background-color: #ffffff;
                    color: #24292e;
                }}
                
                .content-wrapper {{
                    padding: 0;
                }}

                /* 数学公式样式 - 确保正确显示 */
                .math-container {{
                    margin: 10px 0;
                    padding: 10px;
                    text-align: center;
                }}
                
                .math-inline {{
                    display: inline;
                    margin: 0 2px;
                }}
                
                .math-block {{
                    display: block;
                    margin: 15px 0;
                }}
                
                .math-error {{
                    color: #dc2626;
                    background: #fef2f2;
                    border: 1px solid #fecaca;
                    padding: 8px 12px;
                    border-radius: 4px;
                    font-family: monospace;
                }}
                
                /* 代码块容器样式 */
                .code-container {{
                    position: relative;
                    margin: 8px 0;
                    border-radius: 4px;
                    overflow: hidden;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
                }}
                
                /* 代码标题栏 */
                .code-header {{
                    background: #2d3747;
                    color: #e5e7eb;
                    padding: 6px 12px;
                    font-size: 12px;
                    font-weight: 500;
                    border-bottom: 1px solid #3e4c5e;
                }}
                
                /* 代码内容区域 */
                .code-content {{
                    display: flex;
                    background: #1e293b;
                    margin: 0;
                }}
                
                /* 行号样式 */
                .line-numbers {{
                    background: #1a2332;
                    color: #64748b;
                    padding: 8px 0;
                    text-align: right;
                    font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
                    font-size: {code_font_size}px;
                    line-height: {line_height};
                    user-select: none;
                    border-right: 1px solid #334155;
                    min-width: 40px;
                    flex-shrink: 0;
                    padding-right: 8px;
                }}
                
                .line-number {{
                    display: block;
                    height: auto;
                }}
                
                /* 代码区域 */
                .code-wrapper {{
                    flex: 1;
                    overflow-x: auto;
                    padding: 8px 12px;
                }}
                
                .code {{
                    font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
                    font-size: {code_font_size}px;
                    line-height: {line_height};
                    color: #e2e8f0;
                    background: transparent;
                    margin: 0;
                    padding: 0;
                    white-space: pre;
                    tab-size: 4;
                    -moz-tab-size: 4;
                }}
                
                pre {{
                    font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace !important;
                    white-space: pre !important;
                    margin: 0 !important;
                    padding: 0 !important;
                    tab-size: 4 !important;
                    -moz-tab-size: 4 !important;
                    background: transparent !important;
                }}
                
                code {{
                    font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace !important;
                    white-space: pre !important;
                    tab-size: 4 !important;
                    -moz-tab-size: 4 !important;
                    background: transparent !important;
                    font-size: {code_font_size}px !important;
                    line-height: {line_height} !important;
                }}
                
                .hljs {{
                    font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace !important;
                    white-space: pre !important;
                    background: transparent !important;
                    padding: 0 !important;
                    margin: 0 !important;
                    display: block !important;
                    tab-size: 4 !important;
                    -moz-tab-size: 4 !important;
                    font-size: {code_font_size}px !important;
                    line-height: {line_height} !important;
                }}
                
                p {{ margin: 8px 0; }}
                h1, h2, h3, h4, h5, h6 {{ margin: 12px 0 6px 0; }}
                ul, ol {{ margin: 6px 0; padding-left: 24px; }}
                li {{ margin: 2px 0; }}
                blockquote {{ border-left: 2px solid #dfe2e5; padding-left: 12px; margin: 6px 0; color: #6a737d; }}
                table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 12px; }}
                th, td {{ border: 1px solid #dfe2e5; padding: 4px 8px; text-align: left; }}
                th {{ background-color: #f6f8fa; font-weight: 600; }}
            </style>
            
            <!-- 使用 KaTeX - 更快速可靠的数学公式渲染 -->
            <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
            <script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
            <script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
            
            <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/styles/github-dark.min.css">
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/highlight.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/python.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/javascript.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/java.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/cpp.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/c.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/html.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/css.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/sql.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/bash.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/json.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/xml.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/yaml.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.8.0/languages/markdown.min.js"></script>
            
            <script>
                // 高亮代码块
                document.addEventListener('DOMContentLoaded', function() {{
                    hljs.highlightAll();
                    
                    // KaTeX 数学公式渲染
                    renderMathInElement(document.body, {{
                        delimiters: [
                            {{left: '$$', right: '$$', display: true}},
                            {{left: '$', right: '$', display: false}},
                            {{left: '\\\\(', right: '\\\\)', display: false}},
                            {{left: '\\\\[', right: '\\\\]', display: true}}
                        ],
                        throwOnError: false,
                        errorColor: '#cc0000',
                        macros: {{
                            "\\RR": "\\\\mathbb{{R}}",
                            "\\CC": "\\\\mathbb{{C}}", 
                            "\\QQ": "\\\\mathbb{{Q}}",
                            "\\ZZ": "\\\\mathbb{{Z}}",
                            "\\NN": "\\\\mathbb{{N}}",
                            "\\bm": "\\\\boldsymbol{{#1}}",
                            "\\abs": ["\\\\left|#1\\\\right|", 1],
                            "\\norm": ["\\\\left\\\\|#1\\\\right\\\\|", 1]
                        }}
                    }});
                    
                    // 标记渲染完成
                    window.mathRendered = true;
                }});
            </script>
        </head>
        <body>
            <div class="content-wrapper">
                {content}
            </div>
        </body>
        </html>
        """

        # 第一步：Markdown -> HTML
        html_content = mistune.html(md_text)
        
        # 第二步：预处理数学公式
        processed_html = preprocess_math_formulas(html_content)
        
        # 第三步：Python预处理代码块，加上行号
        processed_html = process_code_blocks_in_html(processed_html)
        
        # 第四步：转义花括号
        processed_html = escape_curly_braces(processed_html)
        
        # 第五步：使用安全的格式化生成完整HTML
        full_html = safe_format(
            html_template,
            content=processed_html,
            width_style=width_style,
            code_font_size=code_font_size,
            line_height=line_height
        )


        async with async_playwright() as p:
            logger.info("启动Playwright浏览器...")
            browser = await p.chromium.launch()
            context = await browser.new_context(device_scale_factor=scale)
            page = await context.new_page()

            await page.set_content(full_html, wait_until="domcontentloaded")

            try:
                # 等待高亮完成
                await page.wait_for_function(
                    "document.querySelectorAll('.hljs').length > 0 || document.querySelectorAll('pre code').length === 0",
                    timeout=5000
                )
                
                # 等待 KaTeX 渲染完成
                await page.wait_for_function(
                    "window.mathRendered === true",
                    timeout=10000
                )
                
                # 额外等待确保所有内容渲染完成
                await page.wait_for_timeout(1000)
                
                # 检查数学公式是否渲染成功
                math_elements = await page.query_selector_all('.katex, .katex-display')
                if math_elements:
                    logger.info(f"检测到 {len(math_elements)} 个数学公式元素")
                else:
                    logger.warning("未检测到数学公式元素，可能渲染失败")
                
            except Exception as e:
                logger.warning(f"渲染警告: {e}")
                # 即使有警告也继续，可能部分内容已经渲染完成

            # 截图
            content_element = await page.query_selector('.content-wrapper')
            if content_element:
                bounding_box = await content_element.bounding_box()
                if bounding_box:
                    await page.screenshot(
                        path=output_image_path,
                        clip={
                            'x': bounding_box['x'],
                            'y': bounding_box['y'],
                            'width': bounding_box['width'],
                            'height': bounding_box['height']
                        }
                    )
                else:
                    await page.screenshot(path=output_image_path, full_page=True)
            else:
                await page.screenshot(path=output_image_path, full_page=True)
                
            await browser.close()
            logger.info(f"Markdown 图片已生成: {output_image_path}")
    except Exception as e:
        logger.error(f"Playwright转换过程中发生错误: {e}")
        import traceback
        logger.error(f"Playwright错误堆栈:\n{traceback.format_exc()}")
        raise  # 重新抛出异常以便上层捕获


def preprocess_math_formulas(html_content: str) -> str:
    """
    预处理数学公式，确保 KaTeX 能正确渲染
    """
    import re
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # 处理行内数学公式 $...$
    inline_pattern = re.compile(r'\$([^$]+)\$')
    text_nodes = soup.find_all(text=True)
    
    for text_node in text_nodes:
        if text_node.parent and text_node.parent.name in ['script', 'style']:
            continue
            
        if inline_pattern.search(text_node):
            new_text = inline_pattern.sub(
                r'<span class="math-inline">\\(\1\\)</span>', 
                text_node
            )
            new_soup = BeautifulSoup(new_text, 'html.parser')
            text_node.replace_with(new_soup)
    
    # 处理块级数学公式 $$...$$
    block_pattern = re.compile(r'\$\$(.*?)\$\$', re.DOTALL)
    text_nodes = soup.find_all(text=True)
    
    for text_node in text_nodes:
        if text_node.parent and text_node.parent.name in ['script', 'style']:
            continue
            
        if block_pattern.search(text_node):
            new_text = block_pattern.sub(
                r'<div class="math-block">\\[\1\\]</div>', 
                text_node
            )
            new_soup = BeautifulSoup(new_text, 'html.parser')
            text_node.replace_with(new_soup)
    
    # 处理 LaTeX 环境 \[ ... \] 和 \( ... \)
    latex_inline_pattern = re.compile(r'\\\((.+?)\\\)')
    latex_block_pattern = re.compile(r'\\\[(.+?)\\\]', re.DOTALL)
    
    text_nodes = soup.find_all(text=True)
    for text_node in text_nodes:
        if text_node.parent and text_node.parent.name in ['script', 'style']:
            continue
            
        # 处理行内 \( ... \)
        if latex_inline_pattern.search(text_node):
            new_text = latex_inline_pattern.sub(
                r'<span class="math-inline">\\(\1\\)</span>',
                text_node
            )
            new_soup = BeautifulSoup(new_text, 'html.parser')
            text_node.replace_with(new_soup)
        
        # 处理块级 \[ ... \]
        if latex_block_pattern.search(text_node):
            new_text = latex_block_pattern.sub(
                r'<div class="math-block">\\[\1\\]</div>',
                text_node
            )
            new_soup = BeautifulSoup(new_text, 'html.parser')
            text_node.replace_with(new_soup)
    
    return str(soup)


@register(
    "SmartMd2Img",
    "Daily-AC",
    "智能Markdown转图片插件，自动检测复杂格式并转换为图片",
    "1.0.0",
)
class SmartMarkdownConverterPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.DATA_DIR = os.path.normpath(StarTools.get_data_dir())
        self.IMAGE_CACHE_DIR = os.path.join(self.DATA_DIR, "md2img_cache")
        self.FILE_CACHE_DIR = os.path.join(self.DATA_DIR, "file_cache")
        self.detector = MarkdownComplexityDetector()
        global code_font_size, line_height
        # 从 config 参数获取用户配置
        self.config = config
        code_font_size = self.get_config_value('code_font_size', 13)
        line_height = self.get_config_value('line_height', 1.5)
        logger.info(f"加载用户配置: {self.config}")

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

    def get_config_value(self, key: str, default=None):
        """获取配置值"""
        return self.config.get(key, default)

    @filter.on_llm_request()
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        """向 LLM 说明图片渲染功能的使用方式"""
        auto_detect = self.get_config_value('auto_detect', True)
        
        if auto_detect:
            instruction_prompt = """
            当你需要发送包含复杂格式（如表格、数学公式、LaTeX等）的内容时，请使用 <md> 和 </md> 标签包裹需要转换为图片的Markdown内容。代码块不用加标签，会单独处理。
            例如：
            <md>
            $$E=mc^2$$
            </md>
            <md>
            \(a_1, a_2, \ldots, a_n\)
            </md>
            <md>
            \[
                \left( \sum_{i=1}^{n} a_i b_i \right)^2 \leq \left( \sum_{i=1}^{n} a_i^2 \right) \left( \sum_{i=1}^{n} b_i^2 \right)
            \]
            </md>
            <md>
            \(\mathbf{a} = (a_1, a_2, \ldots, a_n)\) 和 \(\mathbf{b} = (b_1, b_2, \ldots, b_n)\)
            </md>
            """
        else:
            instruction_prompt = """
当你需要发送包含复杂格式（如代码块、表格、数学公式、LaTeX等）的内容时，请使用 <md> 和 </md> 标签包裹需要转换为图片的Markdown内容。

例如：
<md>
# 复杂内容标题
```python
print("Hello World")
```
</md>
<md>
$$E=mc^2$$
</md>
<md>
\(a_1, a_2, \ldots, a_n\)
</md>
<md>
\[
    \left( \sum_{i=1}^{n} a_i b_i \right)^2 \leq \left( \sum_{i=1}^{n} a_i^2 \right) \left( \sum_{i=1}^{n} b_i^2 \right)
\]
</md>
<md>
\(\mathbf{a} = (a_1, a_2, \ldots, a_n)\) 和 \(\mathbf{b} = (b_1, b_2, \ldots, b_n)\)
</md>
"""
        if self.get_config_value("is_debug_mode", False):
            logger.info(f"<DEBUG> LLM 请求说明: {instruction_prompt}")

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
        if self.get_config_value("is_debug_mode", False):
            logger.info(f"<DEBUG> 处理前的消息链: {chain}")
        new_chain = []
        
        for item in chain:
            if isinstance(item, Plain):
                components = await self._smart_process_markdown(item.text)
                new_chain.extend(components)
            else:
                new_chain.append(item)
                
        result.chain = new_chain

    @filter.command("mdconfig")
    async def on_config_command(self, event: AstrMessageEvent):
        """查看当前配置的命令"""
        try:
            config_info = "📋 **智能Markdown转换插件当前配置**\n\n"
            
            # 基础配置
            config_info += "**基础设置**\n"
            auto_detect = self.get_config_value('auto_detect', True)
            complexity_score = self.get_config_value('min_complexity_score', 2)
            respect_md_tags = self.get_config_value('respect_md_tags', True)
            separate_code = self.get_config_value('separate_code_blocks', True)
            separate_math = self.get_config_value('separate_math_blocks', False)
            
            config_info += f"• 自动检测: {'✅ 开启' if auto_detect else '❌ 关闭'}\n"
            config_info += f"• 复杂度阈值: {complexity_score}\n"
            config_info += f"• 尊重MD标签: {'✅ 开启' if respect_md_tags else '❌ 关闭'}\n"
            config_info += f"• 分离代码块: {'✅ 开启' if separate_code else '❌ 关闭'}\n"
            config_info += f"• 分离数学公式: {'✅ 开启' if separate_math else '❌ 关闭'}\n\n"
            
            # 代码处理配置
            config_info += "**代码处理设置**\n"
            render_code = self.get_config_value('render_code_as_image', True)
            send_file = self.get_config_value('send_code_as_file', False)
            file_threshold = self.get_config_value('code_file_threshold', 10)
            
            config_info += f"• 代码渲染为图片: {'✅ 开启' if render_code else '❌ 关闭'}\n"
            config_info += f"• 长代码发送为文件: {'✅ 开启' if send_file else '❌ 关闭'}\n"
            config_info += f"• 文件转换阈值: {file_threshold} 行\n\n"
            
            # 数学公式处理配置
            config_info += "**数学公式处理**\n"
            render_math = self.get_config_value('render_math_as_image', True)
            config_info += f"• 公式渲染为图片: {'✅ 开启' if render_math else '❌ 关闭'}\n\n"
            
            # 支持的语言列表
            supported_langs = self.get_config_value('supported_code_languages', [
                "python", "javascript", "java", "cpp", "c", 
                "html", "css", "sql", "bash", "shell",
                "php", "ruby", "go", "rust", "typescript",
                "json", "xml", "yaml", "markdown"
            ])
            
            config_info += f"**支持的文件语言**\n"
            config_info += f"• 共 {len(supported_langs)} 种: {', '.join(supported_langs[:8])}"
            if len(supported_langs) > 8:
                config_info += f" 等\n"
            else:
                config_info += "\n"
            
            yield event.plain_result(config_info)
            
        except Exception as e:
            logger.error(f"获取配置信息失败: {e}")
            yield event.plain_result("❌ 获取配置信息失败，请检查日志")

    @filter.command("test_code")
    async def on_test_code(self, event: AstrMessageEvent):
        """测试代码渲染效果的命令"""
        try:
            # 测试用的代码示例
            test_code = '''```python
    def hello_world():
        """这是一个测试函数"""
        for i in range(10):
            if i % 2 == 0:
                print(f"偶数: {i}")
            else:
                print(f"奇数: {i}")
        
        # 返回结果
        return "测试完成"

    class TestClass:
        def __init__(self, name):
            self.name = name
        
        def greet(self):
            return f"Hello, {self.name}!"
    ```'''

            # 生成测试图片
            image_filename = f"test_code_{uuid.uuid4().hex[:8]}.png"
            output_path = os.path.join(self.IMAGE_CACHE_DIR, image_filename)
            
            await markdown_to_image_playwright(
                md_text=test_code,
                output_image_path=output_path,
                scale=2,
                width=600
            )
            
            if os.path.exists(output_path):
                yield event.image_result(output_path)
            else:
                yield event.plain_result("测试图片生成失败")
                
        except Exception as e:
            logger.error(f"测试代码渲染失败: {e}")
            yield event.plain_result(f"测试失败: {e}")

    async def _smart_process_markdown(self, text: str) -> List:
        """
        智能处理Markdown文本，自动判断是否需要转换为图片
        """
        components = []

        # 首先处理显式的<md>标签（如果启用）
        respect_md_tags = self.get_config_value('respect_md_tags', True)
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
        separate_code = self.get_config_value('separate_code_blocks', True)
        separate_math = self.get_config_value('separate_math_blocks', False)
        
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
        """处理纯文本（智能分离复杂部分和简单部分）"""
        components = []
        
        # 检查是否主要是链接内容
        if self.detector._only_contains_links(text):
            logger.info("检测到链接内容，保持为文本直接发送")
            components.append(Plain(text))
            return components
        
        # 如果自动检测关闭，直接返回文本
        if not self.get_config_value('auto_detect', True):
            components.append(Plain(text))
            return components
        
        # 检查是否需要渲染
        if not self.detector.needs_rendering(text, self.get_config_value('min_complexity_score', 2)):
            components.append(Plain(text))
            return components
        
        logger.info("检测到复杂Markdown格式，尝试智能分离处理")
        
        # 尝试提取复杂部分并分别处理
        processed_components = await self._extract_and_process_complex_parts(text)
        if processed_components:
            components.extend(processed_components)
        else:
            # 如果分离失败，回退到整体转换
            image_component = await self._convert_markdown_to_image(text)
            if image_component:
                components.append(image_component)
            else:
                components.append(Plain(text))
                
        return components

    async def _extract_and_process_complex_parts(self, text: str) -> List:
        """提取并分别处理复杂部分"""
        components = []
        
        # 使用正则表达式分割文本，识别复杂块
        pattern = r'(```[\s\S]*?```|\$\$[\s\S]*?\$\$|\$[^$]+\$|`[^`]+`|\|.*\|.*\n\|.*---.*\|.*\n(?:\|.*\|.*\n)*)'
        parts = re.split(pattern, text)
        
        current_simple_text = ""
        
        for part in parts:
            if not part:
                continue
                
            # 检查是否为复杂块
            is_complex = (
                part.startswith('```') and part.endswith('```') or  # 代码块
                part.startswith('$$') and part.endswith('$$') or    # 数学公式块
                part.startswith('$') and part.endswith('$') or      # 行内数学公式
                part.startswith('`') and part.endswith('`') or      # 行内代码
                re.match(r'^\|.*\|.*\n\|.*---.*\|.*\n(?:\|.*\|.*\n)*$', part)  # 表格
            )
            
            if is_complex:
                # 先处理累积的简单文本
                if current_simple_text.strip():
                    components.append(Plain(current_simple_text))
                    current_simple_text = ""
                
                # 处理复杂块
                if part.startswith('```') and part.endswith('```'):
                    # 代码块
                    code_match = re.match(r'```(\w+)?\n?(.*?)\n?```', part, re.DOTALL)
                    if code_match:
                        language = code_match.group(1) or 'text'
                        content = code_match.group(2).strip()
                        
                        # 关键修改：规范化缩进
                        normalized_content = self._normalize_code_indentation(content)
                        
                        code_block = {
                            'language': language,
                            'content': normalized_content,  # 使用规范化后的内容
                            'full_match': part,
                            'start': 0,
                            'end': len(part)
                        }
                        code_components = await self._process_code_block(code_block)
                        components.extend(code_components)
                    else:
                        components.append(Plain(part))
                elif part.startswith('$$') and part.endswith('$$'):
                    # 数学公式块
                    math_content = part[2:-2].strip()
                    math_block = {
                        'type': 'block',
                        'content': math_content,
                        'full_match': part,
                        'start': 0,
                        'end': len(part)
                    }
                    math_components = await self._process_math_block(math_block)
                    components.extend(math_components)
                elif part.startswith('$') and part.endswith('$') and len(part) > 2:
                    # 行内数学公式
                    math_content = part[1:-1].strip()
                    math_block = {
                        'type': 'inline',
                        'content': math_content,
                        'full_match': part,
                        'start': 0,
                        'end': len(part)
                    }
                    math_components = await self._process_math_block(math_block)
                    components.extend(math_components)
                elif part.startswith('`') and part.endswith('`') and len(part) > 2:
                    # 行内代码 - 保持为文本
                    components.append(Plain(part))
                elif re.match(r'^\|.*\|.*\n\|.*---.*\|.*\n(?:\|.*\|.*\n)*$', part):
                    # 表格 - 转换为图片
                    image_component = await self._convert_markdown_to_image(part)
                    if image_component:
                        components.append(image_component)
                    else:
                        components.append(Plain(part))
                else:
                    components.append(Plain(part))
            else:
                # 累积简单文本
                current_simple_text += part
        
        # 处理最后剩余的简单文本
        if current_simple_text.strip():
            components.append(Plain(current_simple_text))
        
        return components

    async def _process_code_block(self, code_block: Dict) -> List:
        """处理代码块"""
        render_code = self.get_config_value('render_code_as_image', True)
        send_file = self.get_config_value('send_code_as_file', False)
        file_threshold = self.get_config_value('code_file_threshold', 10)
        
        language = code_block['language']
        content = code_block['content']
        supported_languages = self.get_config_value('supported_code_languages', [
            "python", "javascript", "java", "cpp", "c", 
            "html", "css", "sql", "bash", "shell",
            "php", "ruby", "go", "rust", "typescript",
            "json", "xml", "yaml", "markdown"
        ])
        
        components = []
        
        # 计算代码行数
        line_count = len(content.split('\n'))
        logger.info(f"代码块处理: 语言={language}, 行数={line_count}, 阈值={file_threshold}")
        
        # 检查是否应该发送为文件
        should_send_file = (send_file and 
                        language.lower() in [lang.lower() for lang in supported_languages] and 
                        line_count > file_threshold)
        
        logger.info(f"是否发送文件: {should_send_file}")
        
        # 关键修改：确保代码缩进为4个空格
        # 将制表符转换为4个空格，并确保缩进一致性
        normalized_content = self._normalize_code_indentation(content)
        
        if should_send_file:
            # 发送为文件
            file_component = await self._create_code_file(normalized_content, language)
            if file_component:
                components.append(file_component)
                # 添加简短的代码预览
                preview_lines = normalized_content.split('\n')[:5]  # 显示前5行作为预览
                preview = '\n'.join(preview_lines)
                if line_count > 5:
                    preview += f"\n... (共{line_count}行，完整代码已发送为文件)"
                components.append(Plain(f"```{language}\n{preview}\n```"))
            else:
                # 文件创建失败，回退到渲染或直接发送
                if render_code:
                    md_content = f"```{language}\n{normalized_content}\n```"
                    image_component = await self._convert_markdown_to_image(md_content)
                    if image_component:
                        components.append(image_component)
                    else:
                        components.append(Plain(f"```{language}\n{normalized_content}\n```"))
                else:
                    components.append(Plain(f"```{language}\n{normalized_content}\n```"))
        elif render_code:
            # 渲染为图片
            md_content = f"```{language}\n{normalized_content}\n```"
            image_component = await self._convert_markdown_to_image(md_content)
            if image_component:
                components.append(image_component)
            else:
                components.append(Plain(f"```{language}\n{normalized_content}\n```"))
        else:
            # 直接发送代码文本
            components.append(Plain(f"```{language}\n{normalized_content}\n```"))
        
        return components

    def _normalize_code_indentation(self, code_content: str) -> str:
        """
        规范化代码缩进，确保使用4个空格
        """
        lines = code_content.split('\n')
        normalized_lines = []
        
        for line in lines:
            # 计算前导空格或制表符的数量
            leading_whitespace = len(line) - len(line.lstrip())
            
            if leading_whitespace > 0:
                # 获取前导空白字符
                leading_chars = line[:leading_whitespace]
                
                # 如果是制表符，转换为4个空格
                if '\t' in leading_chars:
                    # 计算制表符等效的空格数（每个制表符=4个空格）
                    tab_count = leading_chars.count('\t')
                    space_count = len(leading_chars) - tab_count
                    total_spaces = tab_count * 4 + space_count
                    
                    # 使用4个空格替换
                    normalized_line = ' ' * total_spaces + line[leading_whitespace:]
                else:
                    # 已经是空格，确保是4的倍数
                    space_count = len(leading_chars)
                    # 如果不是4的倍数，向上取整到最近的4的倍数
                    if space_count % 4 != 0:
                        normalized_space_count = ((space_count + 3) // 4) * 4
                        normalized_line = ' ' * normalized_space_count + line[leading_whitespace:]
                    else:
                        normalized_line = line
            else:
                normalized_line = line
                
            normalized_lines.append(normalized_line)
        
        return '\n'.join(normalized_lines)

    async def _process_math_block(self, math_block: Dict) -> List:
        """处理数学公式块"""
        render_math = self.get_config_value('render_math_as_image', True)
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
            logger.info(f"开始转换Markdown到图片，内容长度: {len(md_content)}")
            if self.get_config_value("is_debug_mode", False):
                logger.info(f"<DEBUG> 转换内容预览: {md_content[:200]}...")
            
            await markdown_to_image_playwright(
                md_text=md_content,
                output_image_path=output_path,
                scale=2,
                width=600
            )
            
            if os.path.exists(output_path):
                file_size = os.path.getsize(output_path)
                logger.info(f"图片生成成功: {output_path} (大小: {file_size} bytes)")
                return Image.fromFileSystem(output_path)
            else:
                logger.error(f"Markdown 图片生成失败: {output_path} 文件不存在")
                return None
                
        except Exception as e:
            # 更详细的错误信息
            import traceback
            error_details = traceback.format_exc()
            logger.error(f"Markdown 转换异常详情: {str(e)}")
            logger.error(f"完整堆栈跟踪:\n{error_details}")
            
            # 检查具体是什么异常
            logger.error(f"异常类型: {type(e).__name__}")
            
            return None


# 配置选项JSON - 扁平化结构
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
    },
    "render_math_as_image": {
        "description": "将数学公式渲染为图片",
        "type": "bool",
        "default": True,
        "hint": "启用后数学公式会转换为图片"
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
    },
    "is_debug_mode": {
    "description": "启用调试模式",
    "type": "bool",
    "default": False,
    "hint": "启用后会记录详细的调试信息"
  },
  "code_font_size": {
    "description": "代码渲染字体大小",
    "type": "int",
    "default": 13,
    "hint": "设置代码块渲染时的字体大小"
  },
  "line_height": {
    "description": "代码渲染行高",
    "type": "float",
    "default": 1.5,
    "hint": "设置代码块渲染时的行高"
  }
}

if __name__ == "__main__":
    # 输出配置JSON
    print("配置选项JSON:")
    print(json.dumps(CONFIG_JSON, ensure_ascii=False, indent=2))
    
