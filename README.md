# Reference Tool

学术论文下载工具集。支持两种模式：从 PDF 批量提取并下载参考文献，以及通过 DOI/标题/URL 快速下载单篇论文。

## 两种使用方式

| | `download_paper.py` | `reference_tool.py` |
|---|---|---|
| 输入 | DOI / 标题 / URL | 源论文 PDF |
| 流程 | 多源搜索 → 直接下载 | 提取参考文献 → 批量下载 |
| 适用 | "帮我把这篇下回来" | "把这篇引用的文献都下了" |
| 技能 | `/paper-download` | `/ref-download` |

## 功能特点

- **多源解析**：出版商直链模板（23家）、Unpaywall OA、arXiv、Semantic Scholar、OpenAlex、Europe PMC
- **多格式引用解析**：数字编号 `[1]`、`1.`、`(1)` 及 APA/MLA 风格
- **并发下载**：多线程 + 连接池复用 + 域名限速
- **二次检索**：Crossref / OpenAlex DOI/URL 反查失败条目
- **PDF 校验重命名**：下载后验证标题匹配，按论文标题重命名
- **站点适配**：Springer、IEEE 等站点 HTML 解析器
- **机构访问**：Cookies 复用机构登录态下载付费内容
- **断点续传**：中断后继续下载
- **图形界面**：Windows GUI 无需命令行
- **234 个单元测试**，覆盖率 56%

## 快速开始

### 安装依赖

```bash
pip install requests pypdf tqdm pdfplumber
```

### 单篇下载（快速）

```bash
# 通过 DOI
python download_paper.py --doi "10.1007/s11071-021-06487-3" --output pdfs/

# 通过标题
python download_paper.py --title "Neural ODE for Power Converter" --output pdfs/

# 通过 URL
python download_paper.py --url "https://arxiv.org/pdf/2301.00001.pdf" --output pdfs/

# 带机构 Cookies 下载付费论文
python download_paper.py --doi "10.1109/TPEL.2023.1234567" --cookies cookies/ieee.txt --output pdfs/
```

### 批量引用下载

```bash
python reference_tool.py --input "你的论文.pdf" --output references_output
```

推荐参数：

```bash
python reference_tool.py --input "你的论文.pdf" --output references_output \
    --workers 12 --secondary-lookup --skip-doi --max-candidates-per-item 2 --retries 1
```

### 图形界面

```bash
python reference_tool_gui.py
```

## 输出文件

### 单篇下载

```
pdfs/
└── paper.pdf              # 下载的 PDF 文件
```

### 批量下载

```
references_output/
├── numbered_references.md   # 编号参考文献列表
├── references.csv           # 结构化表格（含状态）
├── references.json          # 结构化 JSON
├── download_log.csv         # 下载尝试日志
└── downloads/
    ├── meta/                # 条目原文
    ├── verified_pdfs/       # 校验通过的 PDF
    ├── mismatch_pdfs/       # 校验不通过的 PDF
    └── landing_urls/        # 落地页链接
```

## 常用参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--workers` | 并发线程数 | 8 |
| `--timeout` | HTTP 超时（秒） | 20 |
| `--retries` | 重试次数 | 1 |
| `--secondary-lookup` | 启用二次检索 | false |
| `--verify-title-rename` | 校验并按标题重命名 | false |
| `--pdf-parser` | PDF 解析器 (pypdf/pdfplumber) | pdfplumber |
| `--resume` | 断点续传 | true |
| `--skip-doi` | 跳过 DOI 直连 | false |
| `--unpaywall-email` | Unpaywall API 邮箱 | 空 |

## 机构 Cookies

通过 Cookies 复用机构登录态下载付费内容：

1. 浏览器登录机构图书馆
2. 导出 Cookies（JSON 或 Netscape 格式）到 `cookies/`
3. 配置 `domain_cookies.json`：

```json
{
  "link.springer.com": {
    "cookies_path": "cookies/springer.json",
    "description": "学校图书馆 - Springer"
  }
}
```

详细说明见 [docs/cookies_setup_guide.md](docs/cookies_setup_guide.md)。

## 目录结构

```
.
├── download_paper.py               # 单篇快速下载
├── reference_tool.py               # 命令行主程序（参考文献批量下载）
├── reference_tool_gui.py           # GUI 主程序
├── src/
│   ├── models.py                   # 数据模型（ReferenceItem, PipelineConfig 等）
│   ├── parsers.py                  # PDF 引用解析
│   ├── candidates.py               # URL 候选生成与站点适配
│   ├── lookup.py                   # 20 个 API 查找函数
│   ├── _doi_templates.py           # DOI 前缀 → 出版商 PDF URL 映射
│   ├── downloader.py               # 下载管线
│   ├── output.py                   # 输出写入（Markdown/CSV/JSON）
│   └── interactive_ui.py           # 交互式终端 UI
├── core/
│   ├── http.py
│   ├── html.py
│   ├── urls.py
│   └── verify.py
├── site_handlers/
│   ├── registry.py
│   ├── springer.py
│   ├── ieee.py
│   └── domain_analyzer.py
├── tests/                          # 234 个单元测试
├── docs/
├── cookies/                        # 机构 Cookies（不入 git）
└── dist/                           # 打包输出（不入 git）
```

## 开发

### 运行测试

```bash
python -m pytest tests/ -v
# 覆盖率
python -m pytest tests/ -q --cov=src --cov-report=term-missing
```

当前：234 passed，覆盖率 56%。

### 添加新 DOI 模板

编辑 `src/_doi_templates.py`：

```python
DOI_URL_TEMPLATES: list[tuple[str, str]] = [
    # prefix → template, {doi} or {suffix} placeholder
    ("10.1007/", "https://link.springer.com/content/pdf/{doi}.pdf"),
    # ...
]
```

### 添加新查找源

在 `src/lookup.py` 中添加函数：

```python
def lookup_example_pdf_urls_by_title(
    session: requests.Session, expected_title: str, timeout: int
) -> list[str]:
    """通过标题搜索 Example API 获取 PDF 链接。"""
    ...
```

## 许可证

MIT License
