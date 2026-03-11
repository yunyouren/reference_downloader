# 参考文献批量下载工具（论文 PDF）

## 已支持能力
- 多引用格式解析：
  - 数字编号：`[1]`、`1.`、`(1)`
  - 非数字模式（APA/MLA 风格）启发式分段
- 并发下载：`ThreadPoolExecutor` 多线程
- 连接复用：每个工作线程复用 `requests.Session()`
- 进度条：自动使用 `tqdm`（可关闭）
- 更精准 PDF 解析：
  - `pypdf`（默认）
  - `pdfplumber`（可裁剪页眉页脚区域）
- 失败项二次检索：Crossref/OpenAlex 反查 DOI/URL 并重试

## 安装依赖（建议）
```powershell
pip install requests pypdf tqdm pdfplumber
```

## 基础用法
```powershell
python reference_tool.py --input "你的论文.pdf" --output references_output
```

## 配置文件用法（推荐，改参数更方便）
1) 复制一份配置模板并修改：
```powershell
copy reference_tool.config.example.json reference_tool.config.json
```
2) 运行：
```powershell
python reference_tool.py --config reference_tool.config.json
```

也可以直接运行脚本（会自动生成 `reference_tool.config.json`，生成后先编辑再运行）：
```powershell
powershell -ExecutionPolicy Bypass -File .\run_reference_tool.ps1
```

## 使用机构登录态（cookies.txt）尝试下载付费站点 PDF
很多出版方（IEEE/Elsevier/Wiley/AIP/APS 等）会对爬虫/未登录访问返回 403 或 HTML 落地页。若你有合法的机构访问权限，推荐用浏览器登录后导出 `cookies.txt` 再让脚本复用。

1) 先确保浏览器已登录（校园网/VPN/图书馆代理）并能在浏览器里打开目标论文页面
2) 导出 cookies（Netscape 格式）：
   - Chrome/Edge：安装 “Get cookies.txt” 之类扩展，导出 cookies.txt
   - Firefox：安装 “cookies.txt” 扩展导出 cookies.txt
3) 在配置文件里设置：
   - `cookies` 指向你的 cookies.txt 路径
4) 运行脚本即可

注意：cookies.txt 相当于登录态凭证，请不要提交到仓库/发给别人。

## 推荐用法（提速 + 二次检索）
```powershell
python reference_tool.py --input "你的论文.pdf" --output references_output --workers 12 --secondary-lookup --skip-doi --max-candidates-per-item 2 --retries 1
```

## 使用 pdfplumber（减少页眉页脚干扰）
```powershell
python reference_tool.py --input "你的论文.pdf" --output references_output --pdf-parser pdfplumber --header-margin 45 --footer-margin 45
```

## 常用参数
- `--workers`：并发线程数
- `--no-progress`：关闭进度条
- `--pdf-parser {pypdf,pdfplumber}`：切换 PDF 解析后端
- `--header-margin/--footer-margin`：`pdfplumber` 裁剪边距
- `--skip-doi`：初始阶段跳过 DOI 直连
- `--max-candidates-per-item`：每条最多尝试多少链接
- `--download-max`：最多尝试下载多少条（`--initial-max` 仍可用）
- `--cookies`：cookies.txt（Netscape）路径，用于复用登录态
- `--verify-title-rename`：下载后读取 PDF 标题做匹配校验，命中则按标题重命名
- `--verify-title-threshold`：校验阈值（0~1）
- `--verify-title-weight/--verify-line-weight`：校验打分权重
- `--verify-year-hit-bonus/--verify-author-hit-bonus`：年份/作者命中加分
- `--verify-year-miss-mult/--verify-author-miss-mult`：年份/作者未命中乘法惩罚
- `--secondary-lookup`：失败项二次检索
- `--secondary-max`：二次检索最多处理多少条失败项
- `--secondary-cache`：二次检索缓存文件（相对 output 目录）
- `--resume/--no-resume`：断点续跑（复用已有 `references.json` 与已下载文件）
- `--download-log`：下载尝试日志（CSV，传空字符串可禁用）
- `--meta-subdir/--landing-subdir/--mismatch-subdir/--verified-subdir`：downloads 子目录名（空字符串禁用）

## 输出文件
`references_output/` 下：
- `numbered_references.md`：编号参考文献列表
- `references.csv`：结构化表格（含状态）
- `references.json`：结构化 JSON
- `downloads/`：
  - `meta/001_meta.txt`：条目原文
  - `verified_pdfs/001 ... .pdf`：校验通过的 PDF
  - `mismatch_pdfs/001__mismatch.pdf`：校验不通过的 PDF（会继续尝试其他候选）
  - `landing_urls/001_landing.url.txt`：落地页链接
  - `cache/secondary_lookup_cache.json`：二次检索缓存（默认）

## 站点扩展（新增/修改站点逻辑）
站点相关的 HTML 解析与跳转处理放在 `site_handlers/` 目录中：
- `site_handlers/springer.py`
- `site_handlers/ieee.py`

新增站点时，按现有文件写一个 handler 并注册 host 即可。

## 目录结构（简要）
- `reference_tool.py`：命令行入口与主流程（解析→下载→二次检索→导出）
- `core/`：可复用的核心工具
  - `core/http.py`：HTTP 相关工具（`is_probably_pdf`、`parse_retry_after_seconds`）
  - `core/html.py`：站点 HTML 解析工具（如 Springer/IEEE PDF 链接提取）
- `site_handlers/`：站点适配器（HTML 落地页到直链的解析与下载）
  - `registry.py`：handler 注册与分发
  - `springer.py`、`ieee.py`：已适配示例
- `tests/`：最小单元测试
  - `test_core.py`：核心逻辑基础校验
  - `test_html_parsers.py`：站点 HTML 解析校验

说明：handler 与主流程通过“helpers”解耦，站点文件仅依赖 `core/*` 暴露的工具函数，便于后续增加新站点或替换实现。

## Desktop GUI (New)
If you prefer a visual interface, run:
`powershell
python reference_tool_gui.py
`

What the GUI provides:
- Select input PDF, output folder, config file, and cookies file with file pickers.
- Configure common runtime options (workers, timeout, retries, secondary lookup, resume, etc.).
- Load an existing JSON config into the form.
- Save current form settings to a JSON config.
- Run 
eference_tool.py from the GUI and stream live logs.
- Stop a running task from the GUI.
