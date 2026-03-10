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
- `--initial-max`：初始阶段最多处理多少条
- `--secondary-lookup`：失败项二次检索
- `--secondary-max`：二次检索最多处理多少条失败项

## 输出文件
`references_output/` 下：
- `numbered_references.md`：编号参考文献列表
- `references.csv`：结构化表格（含状态）
- `references.json`：结构化 JSON
- `downloads/`：
  - `001_meta.txt`：条目原文
  - `001.pdf`：下载到的 PDF
  - `001_landing.url.txt`：落地页链接
