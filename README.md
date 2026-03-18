# Reference Tool

学术论文参考文献批量下载工具。从 PDF 论文中自动提取引用列表，并尝试下载对应的 PDF 文件。

## 功能特点

- **多格式引用解析**：支持数字编号 `[1]`、`1.`、`(1)` 及 APA/MLA 风格的非数字引用
- **并发下载**：多线程并发下载，支持连接复用
- **二次检索**：对失败条目通过 Crossref/OpenAlex 进行 DOI/URL 反查
- **开放获取支持**：集成 Unpaywall API，优先查找开放获取版本
- **PDF 校验重命名**：下载后验证标题匹配，按论文标题重命名
- **站点适配**：内置 Springer、IEEE 等站点 HTML 解析器
- **机构访问支持**：通过 Cookies 复用机构登录态，下载付费内容
- **断点续传**：支持从上次中断处继续下载
- **图形界面**：提供 Windows GUI 版本，无需命令行操作

## 快速开始

### 安装依赖

```bash
pip install requests pypdf tqdm pdfplumber
```

### 命令行使用

基础用法：

```bash
python reference_tool.py --input "你的论文.pdf" --output references_output
```

推荐参数（提速 + 二次检索）：

```bash
python reference_tool.py --input "你的论文.pdf" --output references_output \
    --workers 12 --secondary-lookup --skip-doi --max-candidates-per-item 2 --retries 1
```

### 配置文件使用（推荐）

1. 复制配置模板：

```bash
copy reference_tool.config.example.json reference_tool.config.json
```

2. 编辑 `reference_tool.config.json`，设置输入文件和参数

3. 运行：

```bash
python reference_tool.py --config reference_tool.config.json
```

### 图形界面

运行 GUI 版本：

```bash
python reference_tool_gui.py
```

或直接使用打包好的可执行文件：

```
dist/ReferenceTool.exe
```

GUI 功能：
- 可视化选择输入 PDF、输出目录、配置文件、Cookies 文件
- 配置常用运行参数（线程数、超时、重试、二次检索等）
- 加载/保存 JSON 配置文件
- 实时显示下载日志
- 支持中英文界面

## 输出文件

运行后在输出目录生成：

```
references_output/
├── numbered_references.md   # 编号参考文献列表
├── references.csv           # 结构化表格（含状态）
├── references.json          # 结构化 JSON
├── download_log.csv         # 下载尝试日志
└── downloads/
    ├── meta/                # 条目原文
    │   └── 001_meta.txt
    ├── verified_pdfs/       # 校验通过的 PDF
    │   └── 001_Title_of_paper.pdf
    ├── mismatch_pdfs/       # 校验不通过的 PDF
    │   └── 001__mismatch.pdf
    └── landing_urls/        # 落地页链接
        └── 001_landing.url.txt
```

## 高级配置

### 使用机构 Cookies

通过配置机构登录态的 Cookies，可以下载需要订阅的期刊内容。

1. 在浏览器中登录机构图书馆（确保能下载 PDF）
2. 使用浏览器扩展导出 Cookies（JSON 或 Netscape 格式）
3. 配置域名映射文件 `domain_cookies.json`：

```json
{
  "version": 1,
  "domains": {
    "link.aps.org": {
      "cookies_path": "cookies/aps.json",
      "description": "学校图书馆 - APS"
    },
    "pubs.acs.org": {
      "cookies_path": "cookies/acs.json",
      "description": "学校图书馆 - ACS"
    }
  }
}
```

详细配置说明见 [cookies_setup_guide.md](cookies_setup_guide.md)。

### 常用参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--workers` | 并发线程数 | 8 |
| `--timeout` | 单次下载超时（秒） | 20 |
| `--retries` | 重试次数 | 1 |
| `--secondary-lookup` | 启用二次检索 | false |
| `--secondary-max` | 二次检索最大条目数 | 40 |
| `--verify-title-rename` | 校验并按标题重命名 | false |
| `--verify-title-threshold` | 标题匹配阈值 | 0.55 |
| `--pdf-parser` | PDF 解析器 (pypdf/pdfplumber) | pypdf |
| `--resume` | 断点续传 | true |
| `--skip-doi` | 跳过 DOI 直连 | false |
| `--max-candidates-per-item` | 每条最大候选链接数 | 3 |
| `--unpaywall-email` | Unpaywall API 邮箱 | 空 |

### 通用下载站点

可在配置文件中自定义下载站点模板：

```json
{
  "generic_download_sites": [
    "https://arxiv.org/search/?query={title_encoded}&searchtype=all",
    "https://www.semanticscholar.org/search?q={title_encoded}",
    "https://core.ac.uk/search?q=doi:{doi_encoded}",
    "https://www.base-search.net/Search/Results?lookfor={title_encoded}&type=all&oaboost=1",
    "https://doaj.org/search/articles/{title_encoded}"
  ]
}
```

支持的占位符：`{doi}`、`{doi_encoded}`、`{title}`、`{title_encoded}`

## 目录结构

```
.
├── reference_tool.py          # 命令行主程序
├── reference_tool_gui.py      # GUI 主程序
├── build_exe.py               # 打包脚本
├── reference_tool.config.json # 配置文件
├── domain_cookies.json        # 域名 Cookies 映射
├── core/                      # 核心工具模块
│   ├── http.py                # HTTP 工具函数
│   ├── html.py                # HTML 解析工具
│   ├── urls.py                # URL 处理工具
│   └── verify.py              # PDF 校验工具
├── site_handlers/             # 站点适配器
│   ├── registry.py            # Handler 注册与分发
│   ├── springer.py            # Springer 站点适配
│   ├── ieee.py                # IEEE 站点适配
│   └── domain_analyzer.py     # 域名分析工具
├── cookies/                   # 预置 Cookies 模板目录
├── tests/                     # 单元测试
└── dist/                      # 打包输出目录
    └── ReferenceTool.exe      # 可执行文件
```

## 开发指南

### 运行测试

```bash
python -m pytest tests/
```

### 添加新站点适配器

1. 在 `site_handlers/` 目录下创建新文件，如 `new_site.py`
2. 实现 PDF 链接提取函数：

```python
def extract_new_site_pdf_url(html: str, url: str) -> str | None:
    """从 HTML 中提取 PDF 直链。"""
    # 解析逻辑
    return pdf_url
```

3. 在 `site_handlers/registry.py` 中注册域名

### 打包可执行文件

```bash
pip install pyinstaller
python build_exe.py
```

打包后的文件位于 `dist/ReferenceTool_Release/` 目录。

## 许可证

MIT License

## 注意事项

1. **Cookies 安全**：Cookies 文件包含登录凭证，请勿分享或提交到版本控制
2. **合理使用**：请遵守出版商的使用条款，仅用于个人学术研究
3. **请求频率**：建议设置适当的 `--min-domain-delay-ms` 避免被限流
4. **Cookies 有效期**：Cookies 通常有过期时间，过期后需重新导出
