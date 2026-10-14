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
- **单元测试与离线下载回归测试**：覆盖参考文献解析、下载校验、续传、Cookie 路由和文件保存

## 下载可靠性与续传说明

- 单篇、批量及 IEEE/Springer/中文期刊站点下载都检查 PDF 文件头和可解析的页面结构；登录页、伪装的 PDF 响应和损坏文件不会仅凭网址后缀或响应类型被当作成功。
- PDF 先写临时文件，校验后保存。同名文件使用 `_2`、`_3` 等后缀，不直接覆盖旧文件；批量下载保留原参考文献编号。
- `saved_landing_url` 只表示保存了网页线索，不表示正文下载完成。再次运行会重试；启用 `--secondary-lookup` 时也会继续检索这些条目。
- 续传同时检查参考文献编号和身份：优先 DOI，否则完整引用文本；不能只凭 `001*.pdf` 判断完成。旧清单缺少身份或准确文件路径时会安全地重新尝试，不删除旧 PDF。建议每篇来源论文使用独立输出文件夹。
- 新的 `references.json` 会为已保存的 PDF 记录 `file_sha256`，续传时检测文件是否改变。旧清单没有哈希时仍可通过身份与结构检查恢复。
- 域名配置的 Cookie 与全局 Cookie 在首轮和二次检索中统一使用，由请求库按 Cookie 的域名、路径和 Secure 属性选择，支持 DOI 跳转到出版社。无域名的 Cookie 不会加入请求。Cookie 仍需有效，且机构本身必须有对应资源的权限。
- 单条任务异常会记录在条目说明与下载日志中，其他条目继续处理。JSON、Markdown 和 CSV 清单采用临时文件替换，避免普通写入失败留下半份清单。

### 第二轮改进

- 题名拼接支持跨行、单独换行的连字符、HTML 实体和连字；结合正文首页 DOI、专利公开编号确认身份，不把参考文献列表中的编号当成本篇身份。拼写容错需要独立作者/年份证据；数值或标识冲突进入待复核，并未降低全局匹配阈值。
- 正文类型与文献身份分开检查。明显的补充材料、预览、仅摘要文件不算正文成功；扫描件、空白件或证据不足的文件保留在 `review_pdfs` 并标记 `needs_review`。所引来源明确为演讲/教程时，允许对应的原始演讲 PDF。
- 二次检索分阶段进行，取得通过校验的正文就停止。按文献线索选择生物医学、化学、图书等专门来源，避免每条引用都查询全部服务。同一轮不会反复尝试已经检查过的候选地址。
- 来源级缓存默认保存 7 天的有效结果、15 分钟的空结果；授权失败、限流、网络及 JSON 解析异常不会当作“没找到全文”写入缓存。缓存保存在输出目录下 `cache/secondary_lookup_cache.json`，空配置仍可关闭缓存。
- 每完成一条即保存独立状态快照和日志，已完成 PDF 的哈希在同一次检查点写入中复用，避免每次重读全部文件。硬关闭后可恢复最后成功写入的完整条目；在途条目可重试。
- GUI 实时接收进度回调，不再全局重定向标准输出。停止按钮取消后续提交，并中断限速/退避等待；当前网络读取可能需要返回或超时后才能结束。状态会区分“正在停止”“已停止”和运行错误。
- 批量请求限速覆盖实际跳转主机及站点内部请求，请求体读完或关闭才释放并发名额。下载落盘、校验后的重命名及移动都使用拒绝覆盖的发布方式；Windows 并发重名不再依赖占位文件再替换。
- JSON/CSV 增加 `document_kind`、`verification_reason`、`verification_version`、`identity_checked`，便于追溯判断依据。旧记录或此前关闭身份校验的记录，在启用校验后会重新检查，不静默沿用旧成功状态。

**边界：** 文献身份和正文分类仍是保守的自动判断，不保证所有出版格式均能正确识别；OCR 尚未接入，疑似扫描件需人工复核。直接 URL 模式未提供题名或 DOI 时，无法与另一份引用信息交叉核对。关闭题名核验不等于关闭正文类型检查。此次没有增加并发数量，也没有进行出版社联网测速。

详见 [第二轮修复说明](docs/reliability-second-round.md)。

离线验证：

```bash
python -m pytest tests -q
```

新增回归测试使用虚构 Cookie、模拟响应和本地 PDF，不需要真实登录凭证。

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
    ├── review_pdfs/         # 扫描件、身份/正文证据不足，待人工复核
    └── landing_urls/        # 落地页链接
```

## 常用参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--workers` | 并发线程数 | 8 |
| `--timeout` | HTTP 超时（秒） | 20 |
| `--retries` | 重试次数 | 1 |
| `--secondary-lookup` | 启用二次检索 | false |
| `--verify-title-rename` | 校验并按标题重命名 | true |
| `--no-verify-title-rename` | 关闭标题校验重命名 | — |
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
