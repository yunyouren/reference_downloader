---
name: paper-download
description: 论文下载工具。支持单篇快速下载（DOI/标题/URL）和参考文献批量下载（PDF/Word 两种输入）。集成多源搜索、Unpaywall 开放获取、机构 Cookies、标题校验重命名。
---

# Paper Download Skill

从 `reference_download/` 工具集，支持两种模式：

| 模式 | 输入 | 脚本 | 适用场景 |
|------|------|------|----------|
| **单篇下载** | DOI / 标题 / URL | `download_paper.py` | "帮我把这篇下回来" |
| **批量引用下载** | 源论文 PDF 或 .docx | `reference_tool.py` / `run_docx_refs.py` | "把这篇的参考文献都下了" |

## 触发条件

### 单篇下载
- "帮我下载这篇论文"
- "下载 DOI 是 xxx 的论文"
- "把这篇论文下回来"
- "找一下这篇论文的 PDF"
- "下载 https://arxiv.org/abs/xxxx"

### 批量引用下载
- "帮我下载这篇论文的参考文献"
- "从这篇 PDF 下载引用文献"
- "批量下载参考文献"
- "把这个 Word 文档里的参考文献下载下来"

## 工具位置

| 功能 | 脚本 |
|------|------|
| 单篇下载（DOI/标题/URL） | `download_paper.py` |
| PDF 批量引用下载 | `reference_tool.py` |
| Word (.docx) 批量引用下载 | `run_docx_refs.py` |
| 图形界面 | `reference_tool_gui.py` |
| 可执行文件 | `dist/ReferenceTool.exe` |

---

## 模式 1：单篇论文下载

```bash
# 通过 DOI（最可靠）
python download_paper.py --doi "10.1007/xxx" --output "pdfs/"

# 通过标题搜索
python download_paper.py --title "论文标题" --output "pdfs/"

# 通过 URL
python download_paper.py --url "https://arxiv.org/pdf/xxxx.pdf" --output "pdfs/"

# 带机构 Cookies 下载付费论文
python download_paper.py --doi "10.1109/xxx" --cookies "cookies/ieee.txt" --output "pdfs/"
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--doi` | 论文 DOI | — |
| `--title` | 论文标题（自动多源搜索） | — |
| `--url` | 直接 PDF 或落地页 URL | — |
| `--output` | 输出目录 | `downloaded_paper/` |
| `--cookies` | Netscape cookies.txt（机构登录） | 无 |
| `--unpaywall-email` | Unpaywall API 邮箱 | 空 |
| `--timeout` | HTTP 超时（秒） | 20 |
| `--max-try` | 最多尝试的候选 URL 数 | 5 |

**解析策略**：DOI → 出版商直链模板(23家) → Unpaywall → doi.org；标题 → arXiv → Semantic Scholar → OpenAlex → Europe PMC → bioRxiv

---

## 模式 2：批量参考文献下载

### 安装依赖
```bash
pip install requests pypdf tqdm pdfplumber python-docx
```

### PDF 输入
```bash
python reference_tool.py --input "论文.pdf" --output "references/" \
    --workers 12 --secondary-lookup --max-candidates-per-item 2 --retries 1
```

### Word (.docx) 输入
```bash
python run_docx_refs.py --input "文献.docx" --output "references/" \
    --workers 12 --secondary-lookup --max-candidates-per-item 2 --retries 1
```

### 输出目录结构
```
references/<论文名>/
├── numbered_references.md   # 编号 + 状态图标 + 落地页链接（一页全览）
├── references.csv / .json   # 结构化数据
├── download_log.csv         # 下载日志
└── downloads/               # 下载的文件
    ├── verified_pdfs/       # 校验通过并按标题重命名的 PDF
    ├── mismatch_pdfs/       # 校验不通过的 PDF
    └── landing_urls/        # 落地页链接
```

### 批量下载参数
| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--workers` | 并发线程数 | 8 |
| `--secondary-lookup` | 启用二次检索（Crossref/OpenAlex） | false |
| `--verify-title-rename` | 校验并按标题重命名 | true |
| `--no-verify-title-rename` | 关闭标题校验重命名 | — |
| `--resume` | 断点续传 | true |
| `--skip-doi` | 跳过 DOI 直连 | false |
| `--unpaywall-email` | Unpaywall API 邮箱 | 空 |
| `--interactive` | 交互式 cookies 配置 | auto |

---

## 通用配置

**机构 Cookies**：导出浏览器登录态到 `cookies/`，编辑 `domain_cookies.json`。
**调试模式**：`REFERENCE_DEBUG=1 python reference_tool.py ...`

## 注意事项
1. **DOI 最可靠**：单篇下载优先使用 DOI
2. **Cookies 安全**：勿分享或提交到版本控制
3. **合理使用**：遵守出版商条款，仅用于学术研究
4. **请求频率**：避免被限流
