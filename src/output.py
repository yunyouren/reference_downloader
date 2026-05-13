"""Output writing functions for the reference download tool.

Writes structured reference data to Markdown, JSON, and CSV formats.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from src.models import ReferenceItem


def write_outputs(refs: list[ReferenceItem], output_dir: Path) -> None:
    """
    将解析/下载结果写入到输出目录。

    输出文件：
    - numbered_references.md: 编号列表 + 下载状态 + 落地页链接
    - references.json: 完整结构化数据（含下载状态）
    - references.csv: 方便用表格软件查看
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    STATUS_ICONS = {
        "downloaded_pdf": "📄",
        "saved_landing_url": "🔗",
        "failed": "❌",
    }

    def _landing_url(item: ReferenceItem) -> str:
        if item.download_status == "saved_landing_url" and item.note:
            return item.note
        return ""

    md_file = output_dir / "numbered_references.md"
    lines = ["# Numbered References", ""]
    for r in refs:
        icon = STATUS_ICONS.get(r.download_status, "❓")
        status = r.download_status.replace("_", " ")
        lines.append(f"- [{r.number}] {icon} `{status}`")
        lines.append(f"  {r.text}")
        if r.downloaded_file:
            lines.append(f"  → {r.downloaded_file}")
        landing = _landing_url(r)
        if landing:
            lines.append(f"  → {landing}")
        lines.append("")
    md_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_file = output_dir / "references.json"
    json_file.write_text(
        json.dumps([asdict(r) for r in refs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    csv_file = output_dir / "references.csv"
    with csv_file.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "number",
                "text",
                "dois",
                "urls",
                "download_status",
                "downloaded_file",
                "note",
            ],
        )
        writer.writeheader()
        for r in refs:
            row = asdict(r)
            row["dois"] = "; ".join(r.dois)
            row["urls"] = "; ".join(r.urls)
            writer.writerow(row)
