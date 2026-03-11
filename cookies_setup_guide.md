# Cookies配置指南

根据下载日志分析，以下期刊需要配置cookies才能下载文献。

---

## 需要配置Cookies的期刊列表

| 期刊名称 | 域名 | 失败次数 | HTTP状态 | 建议cookies文件 |
|---------|------|---------|----------|----------------|
| APS Journals (PhysRev) | link.aps.org | 15 | 403 | cookies/aps.json |
| Cambridge Core | www.cambridge.org | 13 | 429 | cookies/cambridge.json |
| AIAA ARC | arc.aiaa.org | 7 | 403 | cookies/aiaa.json |
| AIP Publishing | pubs.aip.org | 7 | 403 | cookies/aip.json |
| Royal Society | royalsocietypublishing.org | 5 | 403 | cookies/royalsociety.json |
| Annual Reviews | www.annualreviews.org | 4 | 403 | cookies/annualreviews.json |
| ASME Digital Collection | asmedigitalcollection.asme.org | 2 | 403 | cookies/asme.json |
| ACS Publications | pubs.acs.org | 1 | 403 | cookies/acs.json |
| Science (AAAS) | www.science.org | 1 | 403 | cookies/science.json |

---

## 如何获取Cookies

### 方法一：浏览器扩展导出（推荐）

1. **安装浏览器扩展**
   - Chrome/Edge: [Cookie Editor](https://chrome.google.com/webstore/detail/cookie-editor/)
   - Firefox: [Cookie Quick Manager](https://addons.mozilla.org/firefox/addon/cookie-quick-manager/)

2. **登录期刊网站**
   - 通过学校/机构VPN或IP访问期刊网站
   - 确保能正常下载PDF

3. **导出Cookies**
   - 点击扩展图标
   - 选择 "Export" → "JSON" 格式
   - 保存到对应的文件

### 方法二：开发者工具复制

1. 按 `F12` 打开开发者工具
2. 切换到 "Application" → "Cookies"
3. 选择目标网站
4. 手动复制cookies信息

---

## Cookies文件结构

### JSON格式（推荐）

```json
[
  {
    "name": "session_id",
    "value": "abc123...",
    "domain": ".link.aps.org",
    "path": "/",
    "secure": true,
    "httpOnly": false
  }
]
```

### Netscape格式（cookies.txt）

```
.link.aps.org	TRUE	/	TRUE	session_id	abc123...
```

---

## 配置步骤

### 1. 创建cookies目录

```bash
mkdir cookies
```

### 2. 导出各期刊cookies到对应文件

```
cookies/
├── aps.json           # APS Journals
├── cambridge.json     # Cambridge Core
├── aiaa.json          # AIAA ARC
├── aip.json           # AIP Publishing
├── royalsociety.json  # Royal Society
├── annualreviews.json # Annual Reviews
├── asme.json          # ASME Digital Collection
├── acs.json           # ACS Publications
└── science.json       # Science (AAAS)
```

### 3. 配置域名cookies映射

在 `references_output/domain_cookies.json` 中添加：

```json
{
  "version": 1,
  "domains": {
    "link.aps.org": {
      "cookies_path": "cookies/aps.json",
      "description": "学校图书馆 - APS"
    },
    "www.cambridge.org": {
      "cookies_path": "cookies/cambridge.json",
      "description": "学校图书馆 - Cambridge"
    },
    "arc.aiaa.org": {
      "cookies_path": "cookies/aiaa.json",
      "description": "学校图书馆 - AIAA"
    },
    "pubs.aip.org": {
      "cookies_path": "cookies/aip.json",
      "description": "学校图书馆 - AIP"
    },
    "royalsocietypublishing.org": {
      "cookies_path": "cookies/royalsociety.json",
      "description": "学校图书馆 - Royal Society"
    },
    "www.annualreviews.org": {
      "cookies_path": "cookies/annualreviews.json",
      "description": "学校图书馆 - Annual Reviews"
    },
    "asmedigitalcollection.asme.org": {
      "cookies_path": "cookies/asme.json",
      "description": "学校图书馆 - ASME"
    },
    "pubs.acs.org": {
      "cookies_path": "cookies/acs.json",
      "description": "学校图书馆 - ACS"
    },
    "www.science.org": {
      "cookies_path": "cookies/science.json",
      "description": "学校图书馆 - Science"
    }
  }
}
```

### 4. 重新运行下载

```bash
python reference_tool.py --input "Metamaterials and Fluid Flows.pdf" --config reference_tool.config.json
```

---

## 注意事项

1. **Cookies有效期**：Cookies通常有有效期，过期后需要重新导出
2. **安全警告**：不要分享你的cookies文件，它包含你的登录凭证
3. **域名匹配**：确保cookies的domain字段正确（如 `.link.aps.org` 包含前导点）
4. **多机构访问**：如果有多机构访问权限，可以为同一期刊配置不同cookies文件

---

## 常见问题

### Q: 为什么Cambridge返回429错误？
A: 429表示请求过于频繁。即使有cookies，也可能被限流。建议：
- 增加 `--min-domain-delay-ms` 参数（如1000毫秒）
- 减少 `--workers` 并发数

### Q: DOI解析失败怎么办？
A: DOI.org只是解析服务，实际下载需要目标期刊的cookies。

### Q: 如何验证cookies是否有效？
A: 在浏览器中访问期刊的一篇文章，检查是否能下载PDF。如果能，则cookies有效。

---

## 自动生成日期

生成时间：2026-03-11
基于文件：`references_output/download_log.csv`