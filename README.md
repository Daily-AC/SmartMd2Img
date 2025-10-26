# SmartMarkdownConverterPlugin - 智能Markdown转图片插件

一个基于AstrBot框架的智能Markdown转换插件，能够自动检测复杂的Markdown格式并将其转换为图片，确保在QQ等平台中的完美显示。

## ✨ 功能特性

### 🎯 核心功能
- **智能格式检测**：自动检测代码块、表格、数学公式等复杂Markdown格式
- **灵活渲染策略**：支持自动渲染与显式`<md>`标签强制渲染
- **多格式支持**：完美处理LaTeX数学公式、代码高亮、表格等复杂内容

### 💡 智能处理
- **代码块分离**：可将代码块从文本中分离，单独处理
- **数学公式渲染**：支持行内与块级数学公式的图片转换
- **链接保护**：自动识别并保护纯链接内容，避免不必要的转换

### ⚙️ 配置灵活
- **多重处理模式**：提供渲染为图片、发送为文件、混合模式等多种处理方式
- **阈值可调节**：支持根据代码行数智能选择处理方式
- **语言支持**：可配置支持文件发送的编程语言列表

## 🚀 安装指南

### 环境要求
- Python 3.8+
- AstrBot框架
- Playwright Chromium浏览器

### 自动安装
插件会自动安装所需依赖：
```bash
# 插件将自动安装以下依赖：
# - playwright：用于Markdown渲染
# - mistune：用于Markdown解析
```

### 手动安装（可选）
```bash
pip install playwright mistune
python -m playwright install chromium
```

## ⚙️ 配置说明

插件提供丰富的配置选项：

### 基础设置
```json
{
  "auto_detect": {
    "description": "启用自动检测复杂Markdown格式并转换为图片",
    "type": "bool",
    "default": true
  },
  "min_complexity_score": {
    "description": "自动转换的复杂度阈值", 
    "type": "int",
    "default": 2
  }
}
```

### 代码处理设置
```json
{
  "separate_code_blocks": {
    "description": "将代码块从文本中分离处理",
    "type": "bool", 
    "default": true
  },
  "code_handling_settings": {
    "render_code_as_image": true,
    "send_code_as_file": false,
    "code_file_threshold": 10
  }
}
```

### 数学公式处理
```json
{
  "math_handling_settings": {
    "render_math_as_image": true
  }
}
```

## 🎮 使用方法

### 基础使用
插件会自动检测并处理复杂Markdown内容：
- **代码块**：自动检测并转换为图片
- **数学公式**：完美渲染LaTeX公式
- **表格**：保持表格结构清晰显示

### 高级控制
使用`<md>`标签强制转换：
```markdown
这是普通文本，会正常显示。

<md>
# 这是需要渲染为图片的内容
```python
def hello_world():
    print("Hello, World!")
```
</md>
```

### 代码文件发送
当启用文件发送功能时，长代码会自动作为文件发送，并提供代码预览。

## 🔧 处理逻辑

### 复杂度检测
插件会基于以下因素评估是否需要渲染：
- 代码块的数量和复杂度
- 数学公式的存在
- 表格和复杂列表结构
- 文本长度和行长度

### 链接保护机制
以下内容会保持为文本：
- 纯URL链接
- 简单Markdown链接
- 链接占比超过60%的内容

## 🛠️ 技术架构

### 核心组件
- **MarkdownComplexityDetector**：复杂度检测引擎
- **Playwright渲染器**：基于Chromium的高质量渲染
- **MathJax支持**：数学公式渲染
- **文件缓存系统**：高效的资源管理

### 渲染特性
- **高质量排版**：使用GitHub风格的CSS样式
- **代码高亮**：支持多种编程语言语法高亮
- **数学公式**：完整的LaTeX数学公式支持
- **响应式设计**：适配不同屏幕尺寸

## 📝 使用示例

### 混合内容处理
输入：
```markdown
这是一个包含多种元素的示例：

首先是一段Python代码：
```python
def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
```

然后是一个数学公式：
$$E = mc^2$$

最后是一个表格：
| 名称 | 值 |
|------|-----|
| 示例 | 数据 |
```

输出：
- 文本部分保持原样
- 代码块转换为图片或文件
- 数学公式渲染为图片
- 表格转换为图片

## 🔍 故障排除

### 常见问题

**1. 图片生成失败**
- 检查Playwright Chromium是否正确安装
- 验证网络连接（MathJax CDN）

**2. 数学公式显示异常**
- 确认公式语法正确
- 检查特殊字符转义

**3. 文件发送失败**
- 验证文件缓存目录权限
- 检查平台文件发送支持

### 日志调试
启用调试日志查看详细处理过程：
```python
# 在插件日志中查看：
# - 复杂度评分
# - 处理决策
# - 渲染结果
```

## 📄 许可证

本项目采用MIT许可证。

## 🤝 贡献

欢迎提交Issue和Pull Request来改进这个插件！

## 🆕 更新日志

### v1.0.0
- 初始版本发布
- 实现智能Markdown检测
- 支持代码块和数学公式分离处理
- 提供丰富的配置选项

---

**注意**：部分平台可能对文件发送功能支持有限，请根据实际使用情况调整配置。
