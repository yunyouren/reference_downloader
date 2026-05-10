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
	- numbered_references.md: 人类可读的编号列表
	- references.json: 完整结构化数据（含下载状态）
	- references.csv: 方便用表格软件查看（dois/urls 会用 '; ' 拼接）
	"""
	output_dir.mkdir(parents=True, exist_ok=True)

	md_file = output_dir / "numbered_references.md"
	lines = ["# Numbered References", ""]
	for r in refs:
		lines.append(f"[{r.number}] {r.text}")
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
