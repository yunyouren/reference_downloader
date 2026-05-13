---
name: ref-download
description: 参考文献批量下载工具。从 PDF 或 .docx 文件中自动提取引用列表，并尝试下载对应的 PDF 文件。支持 Springer/IEEE 等站点、机构 Cookies、Unpaywall 开放获取、断点续传、标题校验重命名。
---

# Reference Download Skill

从论文中批量提取并下载引用文献。支持 **PDF** 和 **Word (.docx)** 两种输入格式。

## 触发条件

- "帮我下载这篇论文的参考文献"
- "从这篇 PDF 下载引用文献"
- "批量下载参考文献"
- "下载引用论文"
- "把这个 Word 文档里的参考文献下载下来"
- "从文献.docx 下载参考文献"

> 如果只是想下载某篇特定论文（而非批量引用），请使用 `/paper-download`

## 与 paper-download 的区别

| | `/ref-download` | `/paper-download` |
|---|---|---|
| 输入 | 源论文 PDF 或 .docx | DOI、标题、URL |
| 流程 | 提取参考文献 → 批量下载 | 直接搜索+下载 |
| 适用 | "把这篇的参考文献都下了" | "帮我把这篇下回来" |

## 工具位置

- **命令行 (PDF)**: `reference_tool.py`
- **命令行 (Word)**: `run_docx_refs.py`
- **GUI**: `reference_tool_gui.py`
- **可执行文件**: `dist/ReferenceTool.exe`
- **配置模板**: `reference_tool.config.example.json`

## 工作流

### 步骤 1: 确认输入

与用户确认：

1. **输入文件**：源论文 PDF 或 Word 文档
2. **输出目录**：默认 `references/<论文名>/`
3. **高级选项**（可选）：
   - 是否启用二次检索（Crossref/OpenAlex）
   - 是否使用机构 Cookies
   - 并发线程数（默认 8）

### 步骤 2: 安装依赖（首次使用）

```bash
pip install requests pypdf tqdm pdfplumber python-docx
```

### 步骤 3: 运行下载

#### PDF 输入（推荐参数）

```bash
python reference_tool.py \
    --input "论文.pdf" \
    --output "references/论文名" \
    --workers 12 \
    --secondary-lookup \
    --skip-doi \
    --max-candidates-per-item 2 \
    --retries 1
```

#### Word (.docx) 输入

```bash
python run_docx_refs.py \
    --input "文献.docx" \
    --output "references/论文名" \
    --workers 12 \
    --secondary-lookup \
    --max-candidates-per-item 2 \
    --retries 1
```

#### 配置文件模式

```bash
python reference_tool.py --config reference_tool.config.json
```

#### GUI 模式

```bash
python reference_tool_gui.py
```

### 步骤 4: 查看结果

输出目录结构：

```
references/<论文名>/
├── numbered_references.md   # 编号 + 状态图标 + 落地页链接（一页全览）
├── references.csv           # 结构化表格（含状态）
├── references.json          # 结构化 JSON
├── download_log.csv         # 下载尝试日志
└── downloads/
    ├── meta/                # 条目原文
    ├── verified_pdfs/       # 校验通过并按标题重命名的 PDF
    ├── mismatch_pdfs/       # 校验不通过的 PDF
    └── landing_urls/        # 落地页链接
```

`numbered_references.md` 一个文件包含全部信息：
- 📄 已下载 → 显示重命名后的文件名
- 🔗 未下载 → 直接显示落地页 URL
- ❌ 失败 → 标记状态

## 常用参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--workers` | 并发线程数 | 8 |
| `--timeout` | 单次下载超时（秒） | 20 |
| `--retries` | 重试次数 | 1 |
| `--secondary-lookup` | 启用二次检索（Crossref/OpenAlex） | false |
| `--verify-title-rename` | 校验并按标题重命名 | true |
| `--no-verify-title-rename` | 关闭标题校验重命名 | — |
| `--pdf-parser` | PDF 解析器 (pypdf/pdfplumber) | pypdf |
| `--resume` | 断点续传 | true |
| `--skip-doi` | 跳过 DOI 直连 | false |
| `--unpaywall-email` | Unpaywall API 邮箱 | 空 |
| `--interactive` | 交互式 cookies 配置 (auto/true/false) | auto |

## 机构 Cookies 配置

通过 Cookies 复用机构登录态，下载付费内容：

1. 浏览器登录机构图书馆
2. 导出 Cookies（JSON 格式）到 `cookies/`
3. 编辑 `domain_cookies.json` 配置域名映射

详细说明见 `docs/cookies_setup_guide.md`。

## 与 scholar-search 联动

1. 先用 `/scholar-search` 搜索并下载核心论文
2. 再用本工具从核心论文批量下载引用文献

## 注意事项

1. **Cookies 安全**：勿分享或提交到版本控制
2. **合理使用**：遵守出版商条款，仅用于学术研究
3. **请求频率**：避免被限流
4. **调试模式**：设置环境变量 `REFERENCE_DEBUG=1` 可看到 API 异常细节
