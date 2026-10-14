"""Output writing functions for the reference download tool.

Writes structured reference data to Markdown, JSON, and CSV formats.
"""

from __future__ import annotations

import csv
import json
import io
from dataclasses import asdict
from pathlib import Path

from src.models import ReferenceItem
from core.pdf_files import contained_file, file_sha256, atomic_write_text as _atomic_write_text


def write_outputs(refs: list[ReferenceItem], output_dir: Path, *, hash_cache: dict | None = None) -> None:
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
        "needs_review": "🔎",
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
    _atomic_write_text(md_file, "\n".join(lines) + "\n")

    json_file = output_dir / "references.json"
    rows = []
    for item in refs:
        row = asdict(item)
        if item.download_status in {"downloaded_pdf", "needs_review"}:
            pdf = contained_file(output_dir / "downloads", item.downloaded_file)
            if pdf is not None:
                stat = pdf.stat()
                key = (str(pdf), stat.st_size, stat.st_mtime_ns)
                value = hash_cache.get(key) if hash_cache is not None else None
                if value is None:
                    value = file_sha256(pdf)
                    if hash_cache is not None:
                        hash_cache[key] = value
                row["file_sha256"] = value
        rows.append(row)
    _atomic_write_text(json_file, json.dumps(rows, ensure_ascii=False, indent=2))

    csv_file = output_dir / "references.csv"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=["number", "text", "dois", "urls", "download_status", "downloaded_file", "note",
                    "document_kind", "verification_reason", "verification_version", "identity_checked"],
    )
    writer.writeheader()
    for r in refs:
        row = asdict(r)
        row["dois"] = "; ".join(r.dois)
        row["urls"] = "; ".join(r.urls)
        writer.writerow(row)
    _atomic_write_text(csv_file, "\ufeff" + buffer.getvalue())
