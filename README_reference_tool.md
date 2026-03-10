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
- `--secondary-lookup`：失败项二次检索
- `--secondary-max`：二次检索最多处理多少条失败项
- `--download-log`：下载尝试日志（CSV，传空字符串可禁用）

## 输出文件
`references_output/` 下：
- `numbered_references.md`：编号参考文献列表
- `references.csv`：结构化表格（含状态）
- `references.json`：结构化 JSON
- `downloads/`：
  - `001_meta.txt`：条目原文
  - `001.pdf`：下载到的 PDF
  - `001_landing.url.txt`：落地页链接
