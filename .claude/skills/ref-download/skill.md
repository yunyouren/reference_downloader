---
name: ref-download
description: 参考文献批量下载工具。从 PDF 论文中自动提取引用列表，并尝试下载对应的 PDF 文件。支持 Springer/IEEE 等站点、机构 Cookies、Unpaywall 开放获取、断点续传。
---

# Reference Download Skill

从 PDF 论文中批量提取并下载引用文献。

## 触发条件

- "帮我下载这篇论文的参考文献"
- "从这篇 PDF 下载引用文献"
- "批量下载参考文献"
- "下载引用论文"

> 如果只是想下载某篇特定论文（而非批量引用），请使用 `/paper-download`

## 与 paper-download 的区别

| | `/ref-download` | `/paper-download` |
|---|---|---|
| 输入 | 源论文 PDF | DOI、标题、URL |
| 流程 | 提取参考文献 → 批量下载 | 直接搜索+下载 |
| 适用 | "把这篇的参考文献都下了" | "帮我把这篇下回来" |

## 工具位置

脚本位于 `reference_download/` 目录：
- **命令行**: `reference_download/reference_tool.py`
- **GUI**: `reference_download/reference_tool_gui.py`
- **可执行文件**: `reference_download/dist/ReferenceTool.exe`
- **配置模板**: `reference_download/reference_tool.config.example.json`

## 工作流

### 步骤 1: 确认输入

与用户确认：

1. **输入 PDF**：源论文路径（优先从 `pdfs/` 目录选取）
2. **输出目录**：默认 `references/<论文名>/`
3. **高级选项**（可选）：
   - 是否启用二次检索（Crossref/OpenAlex）
   - 是否使用机构 Cookies
   - 并发线程数（默认 8）

### 步骤 2: 安装依赖（首次使用）

```bash
pip install requests pypdf tqdm pdfplumber
```

### 步骤 3: 运行下载

#### 命令行模式（推荐）

```bash
# 基础用法
python reference_download/reference_tool.py \
    --input "pdfs/你的论文.pdf" \
    --output "references/论文名"

# 推荐参数（提速 + 二次检索）
python reference_download/reference_tool.py \
    --input "pdfs/你的论文.pdf" \
    --output "references/论文名" \
    --workers 12 \
    --secondary-lookup \
    --skip-doi \
    --max-candidates-per-item 2 \
    --retries 1
```

#### 配置文件模式

```bash
# 1. 复制配置模板
cp reference_download/reference_tool.config.example.json reference_download/reference_tool.config.json

# 2. 编辑配置文件设置输入和参数

# 3. 运行
python reference_download/reference_tool.py --config reference_download/reference_tool.config.json
```

#### GUI 模式

```bash
python reference_download/reference_tool_gui.py
```

或直接运行：`reference_download/dist/ReferenceTool.exe`

### 步骤 4: 查看结果

输出目录结构：

```
references/<论文名>/
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
| `--timeout` | 单次下载超时（秒） | 20 |
| `--retries` | 重试次数 | 1 |
| `--secondary-lookup` | 启用二次检索 | false |
| `--verify-title-rename` | 校验并按标题重命名 | true |
| `--no-verify-title-rename` | 关闭标题校验重命名 | — |
| `--pdf-parser` | PDF 解析器 (pypdf/pdfplumber) | pypdf |
| `--resume` | 断点续传 | true |
| `--skip-doi` | 跳过 DOI 直连 | false |
| `--unpaywall-email` | Unpaywall API 邮箱 | 空 |

## 机构 Cookies 配置

通过 Cookies 复用机构登录态，下载付费内容：

1. 浏览器登录机构图书馆
2. 导出 Cookies（JSON 格式）到 `reference_download/cookies/`
3. 编辑 `reference_download/domain_cookies.json` 配置域名映射

详细说明见 `reference_download/cookies_setup_guide.md`。

## 与 scholar-search 联动

1. 先用 `/scholar-search` 搜索并下载核心论文到 `pdfs/`
2. 再用本工具从核心论文批量下载引用文献到 `references/`

## 注意事项

1. **Cookies 安全**：勿分享或提交到版本控制
2. **合理使用**：遵守出版商条款，仅用于学术研究
3. **请求频率**：避免被限流
