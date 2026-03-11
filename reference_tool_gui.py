#!/usr/bin/env python3
"""Desktop GUI for reference_tool.py."""

from __future__ import annotations

import json
import importlib.util
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from core.verify import verify_and_rename_pdf
from reference_tool import (
    guess_title_query,
    load_config_file,
    parse_first_author_surname,
    parse_ref_year,
)

STATUS_ORDER = ["downloaded_pdf", "saved_landing_url", "failed", "not_attempted"]

I18N = {
    "zh": {
        "title": "参考文献工具 GUI",
        "input": "输入 PDF",
        "output": "输出目录",
        "config": "配置文件",
        "cookies": "Cookies",
        "run": "运行",
        "stop": "停止",
        "load": "加载配置",
        "save": "保存配置",
        "refresh": "刷新统计",
        "open_output": "打开输出目录",
        "logs": "实时日志",
        "summary": "运行统计",
        "running": "运行中...",
        "ready": "就绪",
        "finished": "已完成",
        "stopped": "已停止",
        "invalid": "参数错误",
    },
    "en": {
        "title": "Reference Tool GUI",
        "input": "Input PDF",
        "output": "Output Dir",
        "config": "Config",
        "cookies": "Cookies",
        "run": "Run",
        "stop": "Stop",
        "load": "Load Config",
        "save": "Save Config",
        "recommend": "Recommended Preset",
        "refresh": "Refresh Summary",
        "open_output": "Open Output",
        "rename_only": "Rename Only",
        "logs": "Live Logs",
        "summary": "Run Summary",
        "running": "Running...",
        "ready": "Ready",
        "finished": "Finished",
        "stopped": "Stopped",
        "invalid": "Invalid settings",
    },
}

RENAME_MODE_LABELS = {
    "en": {
        "original": "Original Name",
        "number_only": "Number Only",
        "number_and_original": "Number + Original",
    },
    "zh": {
        "original": "原名",
        "number_only": "仅编号",
        "number_and_original": "编号+原名",
    },
}


def recommended_download_preset() -> dict[str, Any]:
    return {
        "pdf_parser": "pdfplumber",
        "secondary_lookup": True,
        "secondary_max": 60,
        "secondary_top_k": 3,
        "max_candidates_per_item": 5,
        "retries": 2,
        "timeout": 25,
    }


def is_pdfplumber_available() -> bool:
    return importlib.util.find_spec("pdfplumber") is not None


def rename_mode_labels_for_lang(lang: str) -> list[str]:
    key = "zh" if str(lang).lower().startswith("zh") else "en"
    labels = RENAME_MODE_LABELS[key]
    return [labels["original"], labels["number_only"], labels["number_and_original"]]


def rename_mode_value_to_label(value: str, lang: str) -> str:
    key = "zh" if str(lang).lower().startswith("zh") else "en"
    normalized = str(value or "number_and_original").strip().lower()
    if normalized not in {"original", "number_only", "number_and_original"}:
        normalized = "number_and_original"
    return RENAME_MODE_LABELS[key][normalized]


def rename_mode_label_to_value(label: str, lang: str) -> str:
    key = "zh" if str(lang).lower().startswith("zh") else "en"
    target = str(label or "").strip()
    mapping = RENAME_MODE_LABELS[key]
    for value, text in mapping.items():
        if text == target:
            return value
    # fallback: allow raw english values
    normalized = target.lower()
    if normalized in {"original", "number_only", "number_and_original"}:
        return normalized
    return "number_and_original"


def build_parameter_help_text(lang: str = "en") -> str:
    if str(lang).lower().startswith("zh"):
        return (
            "参数说明\n"
            "------------------------------\n"
            "pdf_parser: PDF 解析器，pypdf 或 pdfplumber。\n"
            "secondary_lookup: 失败条目启用二次检索（Crossref/OpenAlex）。\n"
            "secondary_max: 二次检索最多处理多少失败条目。\n"
            "secondary_top_k: 二次检索每条保留前 K 个候选。\n"
            "max_candidates: 每条参考文献最多尝试多少候选链接。\n"
            "workers: 并发下载线程数。\n"
            "timeout: 单次请求超时（秒）。\n"
            "retries: 每个候选链接重试次数。\n"
            "verify_threshold: 标题匹配阈值（0~1）。\n"
            "verify_rename: 开启下载后校验与重命名。\n"
            "generic_download_sites: 通用下载站点模板（可自定义，如 Sci-Hub）。\n"
            "rename_mode: original/number_only/number_and_original。\n"
            "  original: 仅保留原始标题文件名。\n"
            "  number_only: 仅保留参考文献编号（如 001.pdf）。\n"
            "  number_and_original: 编号+原始标题（如 001 xxx.pdf）。\n"
            "\n"
            "默认推荐参数\n"
            "------------------------------\n"
            "pdf_parser=pdfplumber（未安装时自动回退 pypdf），\n"
            "secondary_lookup=true，secondary_max=60，secondary_top_k=3，\n"
            "max_candidates=5，retries=2，timeout=25。\n"
        )
    return (
        "Parameter Guide\n"
        "------------------------------\n"
        "pdf_parser: pypdf or pdfplumber.\n"
        "secondary_lookup: retry failed refs via Crossref/OpenAlex.\n"
        "secondary_max: max failed refs to process in secondary phase.\n"
        "secondary_top_k: keep top-K lookup candidates per ref.\n"
        "max_candidates: max URL/DOI attempts per reference.\n"
        "workers: concurrent download workers.\n"
        "timeout: per-request timeout in seconds.\n"
        "retries: retries per candidate URL.\n"
        "verify_threshold: title-match threshold (0~1).\n"
        "verify_rename: verify PDF title and rename/move to verified folder.\n"
        "generic_download_sites: custom generic site templates (e.g. Sci-Hub).\n"
        "rename_mode: original / number_only / number_and_original.\n"
        "  original: keep original title-based file name.\n"
        "  number_only: only keep reference number (e.g., 001.pdf).\n"
        "  number_and_original: number + original title (e.g., 001 xxx.pdf).\n"
        "\n"
        "Recommended Defaults\n"
        "------------------------------\n"
        "pdf_parser=pdfplumber (fallback to pypdf if missing),\n"
        "secondary_lookup=true, secondary_max=60, secondary_top_k=3,\n"
        "max_candidates=5, retries=2, timeout=25.\n"
    )


def summarize_references_payload(rows: Any) -> dict[str, int]:
    counts = {status: 0 for status in STATUS_ORDER}
    counts["total"] = 0
    counts["resolved_by_secondary_lookup"] = 0
    if not isinstance(rows, list):
        return counts
    counts["total"] = len(rows)
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("download_status") or "not_attempted")
        if status not in counts:
            status = "not_attempted"
        counts[status] += 1
        if "resolved_by=secondary_lookup" in str(row.get("note") or ""):
            counts["resolved_by_secondary_lookup"] += 1
    return counts


def load_summary_from_output(output_dir: Path) -> dict[str, int]:
    path = output_dir / "references.json"
    if not path.exists():
        return summarize_references_payload([])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return summarize_references_payload([])
    return summarize_references_payload(data)


def load_gui_config_payload(path: Path) -> dict[str, Any]:
    data = load_config_file(path)
    if not isinstance(data, dict):
        raise ValueError("config must be object")
    return data


COOKIE_DOMAIN_PRESETS: dict[str, list[str]] = {
    "aps": ["link.aps.org"],
    "cambridge": ["www.cambridge.org"],
    "aiaa": ["arc.aiaa.org"],
    "aip": ["pubs.aip.org"],
    "royalsociety": ["royalsocietypublishing.org"],
    "annualreviews": ["www.annualreviews.org"],
    "asme": ["asmedigitalcollection.asme.org"],
    "acs": ["pubs.acs.org"],
    "science": ["www.science.org"],
    "ieee": ["ieeexplore.ieee.org"],
    "elsevier": ["linkinghub.elsevier.com", "www.sciencedirect.com"],
    "springer": ["link.springer.com"],
    "wiley": ["onlinelibrary.wiley.com"],
}


def build_domain_cookies_config_from_folder(cookies_dir: Path) -> dict[str, Any]:
    return build_domain_cookies_config_from_folder_with_presets(cookies_dir, None)


def normalize_cookie_domain_presets(raw: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        journal = str(key or "").strip().lower()
        if not journal:
            continue
        domains_raw: list[str]
        if isinstance(value, str):
            domains_raw = [x.strip() for x in value.split(",")]
        elif isinstance(value, list):
            domains_raw = [str(x).strip() for x in value]
        else:
            continue
        domains: list[str] = []
        seen: set[str] = set()
        for domain in domains_raw:
            d = domain.lower()
            if not d or d in seen:
                continue
            seen.add(d)
            domains.append(d)
        if domains:
            out[journal] = domains
    return out


def build_domain_cookies_config_from_folder_with_presets(
    cookies_dir: Path,
    custom_presets: Any,
) -> dict[str, Any]:
    domains: dict[str, dict[str, str]] = {}
    presets: dict[str, list[str]] = dict(COOKIE_DOMAIN_PRESETS)
    presets.update(normalize_cookie_domain_presets(custom_presets))
    for p in cookies_dir.glob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".json", ".txt"}:
            continue
        key = p.stem.strip().lower()
        if key not in presets:
            continue
        for domain in presets[key]:
            domains[domain] = {"cookies_path": str(p.resolve()), "description": f"from {p.name}"}
    return {"version": 1, "domains": domains}


def run_rename_only_on_output(
    *,
    output_dir: Path,
    verify_threshold: float,
    rename_mode: str,
    verified_subdir: str = "verified_pdfs",
    mismatch_subdir: str = "mismatch_pdfs",
) -> dict[str, int]:
    refs_path = output_dir / "references.json"
    if not refs_path.exists():
        raise FileNotFoundError(f"references.json not found in: {output_dir}")

    rows = json.loads(refs_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("references.json must be a list")

    downloads_dir = output_dir / "downloads"
    verified_dir = downloads_dir / verified_subdir
    mismatch_dir = downloads_dir / mismatch_subdir
    stats = {"processed": 0, "renamed_ok": 0, "mismatch": 0, "skipped": 0}

    for row in rows:
        if not isinstance(row, dict):
            stats["skipped"] += 1
            continue
        if str(row.get("download_status") or "") != "downloaded_pdf":
            stats["skipped"] += 1
            continue
        rel = str(row.get("downloaded_file") or "").strip()
        if not rel:
            stats["skipped"] += 1
            continue
        out_file = downloads_dir / rel
        if not out_file.exists() or not out_file.is_file() or out_file.suffix.lower() != ".pdf":
            stats["skipped"] += 1
            continue
        try:
            num = int(row.get("number"))
        except Exception:
            stats["skipped"] += 1
            continue

        prefix = f"{num:03d}"
        ref_text = str(row.get("text") or "")
        decision = verify_and_rename_pdf(
            prefix=prefix,
            out_file=out_file,
            downloads_dir=downloads_dir,
            verified_dir=verified_dir,
            mismatch_dir=mismatch_dir,
            expected_title=guess_title_query(ref_text),
            ref_year=parse_ref_year(ref_text),
            surname=parse_first_author_surname(ref_text),
            verify_title_threshold=float(verify_threshold),
            verify_weights=None,
            verify_rename_mode=str(rename_mode or "number_and_original"),
        )
        stats["processed"] += 1
        if decision.outcome == "downloaded_pdf":
            row["download_status"] = "downloaded_pdf"
            row["downloaded_file"] = decision.rel_path
            row["note"] = f"{str(row.get('note') or '').strip()} | rename_only_ok".strip(" |")
            stats["renamed_ok"] += 1
        else:
            row["download_status"] = "failed"
            row["downloaded_file"] = decision.rel_path
            row["note"] = f"{str(row.get('note') or '').strip()} | rename_only_mismatch".strip(" |")
            stats["mismatch"] += 1

    refs_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


class ReferenceToolGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.base_dir = Path(__file__).resolve().parent
        self.script_path = self.base_dir / "reference_tool.py"
        self.proc: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.temp_config_path: Path | None = None

        self.lang_var = tk.StringVar(value="zh")
        self.input_var = tk.StringVar(value="")
        self.output_var = tk.StringVar(value="references_output")
        self.config_var = tk.StringVar(value="")
        self.cookies_var = tk.StringVar(value="")
        self.cookies_folder_var = tk.StringVar(value="cookies")
        self.domain_cookies_file_var = tk.StringVar(value="domain_cookies.json")
        self.custom_cookie_domain_presets: dict[str, list[str]] = {}
        self.generic_download_sites: list[str] = []
        self.workers_var = tk.StringVar(value="8")
        self.timeout_var = tk.StringVar(value="20")
        self.retries_var = tk.StringVar(value="1")
        self.max_candidates_var = tk.StringVar(value="3")
        self.pdf_parser_var = tk.StringVar(value="pypdf")
        self.secondary_lookup_var = tk.BooleanVar(value=False)
        self.secondary_max_var = tk.StringVar(value="40")
        self.secondary_top_k_var = tk.StringVar(value="2")
        self.verify_rename_var = tk.BooleanVar(value=False)
        self.verify_rename_mode_var = tk.StringVar(value=rename_mode_value_to_label("number_and_original", self.lang_var.get()))
        self.verify_threshold_var = tk.StringVar(value="0.55")
        self.status_var = tk.StringVar(value="")

        self._apply_recommended_defaults(notify_if_fallback=False)
        self._build_ui()
        self._refresh_summary()
        self._set_status("ready")
        self._pump_logs()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _tr(self, key: str) -> str:
        lang = self.lang_var.get() if self.lang_var.get() in I18N else "en"
        return I18N[lang].get(key, key)

    def _set_status(self, key: str) -> None:
        self.status_var.set(self._tr(key))

    def _on_lang_change(self, *_: Any) -> None:
        self._refresh_rename_mode_labels()
        self._refresh_help_text()

    def _current_rename_mode_value(self) -> str:
        return rename_mode_label_to_value(self.verify_rename_mode_var.get(), self.lang_var.get())

    def _set_rename_mode_from_value(self, value: str) -> None:
        self.verify_rename_mode_var.set(rename_mode_value_to_label(value, self.lang_var.get()))

    def _refresh_rename_mode_labels(self) -> None:
        if not hasattr(self, "rename_mode_combo"):
            return
        current_value = self._current_rename_mode_value()
        self.rename_mode_combo.configure(values=rename_mode_labels_for_lang(self.lang_var.get()))
        self._set_rename_mode_from_value(current_value)

    def _refresh_help_text(self) -> None:
        if not hasattr(self, "help_text"):
            return
        self.help_text.configure(state=tk.NORMAL)
        self.help_text.delete("1.0", tk.END)
        self.help_text.insert("1.0", build_parameter_help_text(self.lang_var.get()))
        self.help_text.configure(state=tk.DISABLED)

    def _build_ui(self) -> None:
        self.root.title(self._tr("title"))
        self.root.geometry("1280x820")
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        lang_row = ttk.Frame(frame)
        lang_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(lang_row, text="Language").pack(side=tk.LEFT)
        ttk.Combobox(lang_row, textvariable=self.lang_var, values=["en", "zh"], state="readonly", width=8).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.lang_var.trace_add("write", self._on_lang_change)

        top = ttk.Frame(frame)
        top.pack(fill=tk.BOTH, expand=False)

        left = ttk.Frame(top)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right = ttk.LabelFrame(top, text="Parameter Help")
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))

        self.help_text = ScrolledText(right, width=44, height=26, wrap=tk.WORD)
        self.help_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._refresh_help_text()

        settings = ttk.Frame(left)
        settings.pack(fill=tk.X)
        ttk.Label(settings, text=self._tr("input")).grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.input_var, width=92).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_input).grid(row=0, column=2)
        ttk.Label(settings, text=self._tr("output")).grid(row=1, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.output_var, width=92).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_output).grid(row=1, column=2)
        ttk.Label(settings, text=self._tr("config")).grid(row=2, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.config_var, width=92).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_config).grid(row=2, column=2)
        ttk.Label(settings, text=self._tr("cookies")).grid(row=3, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.cookies_var, width=92).grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_cookies).grid(row=3, column=2)
        ttk.Label(settings, text="Cookies Folder").grid(row=4, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.cookies_folder_var, width=92).grid(row=4, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_cookies_folder).grid(row=4, column=2)
        ttk.Label(settings, text="Domain Cookies File").grid(row=5, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.domain_cookies_file_var, width=92).grid(row=5, column=1, sticky="ew", padx=6)
        domain_actions = ttk.Frame(settings)
        domain_actions.grid(row=5, column=2, sticky="e")
        ttk.Button(domain_actions, text="Build", width=6, command=self._build_domain_cookies_file).pack(side=tk.LEFT)
        ttk.Button(domain_actions, text="Edit Map", width=10, command=self._edit_custom_cookie_domains).pack(side=tk.LEFT, padx=(4, 0))
        settings.grid_columnconfigure(1, weight=1)

        row2 = ttk.Frame(left)
        row2.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(row2, text="workers").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.workers_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row2, text="timeout").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.timeout_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row2, text="retries").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.retries_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row2, text="max_candidates").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.max_candidates_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row2, text="verify_threshold").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.verify_threshold_var, width=8).pack(side=tk.LEFT, padx=(4, 10))

        row3 = ttk.Frame(left)
        row3.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row3, text="pdf_parser").pack(side=tk.LEFT)
        ttk.Combobox(
            row3,
            textvariable=self.pdf_parser_var,
            values=["pypdf", "pdfplumber"],
            state="readonly",
            width=14,
        ).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Checkbutton(row3, text="secondary_lookup", variable=self.secondary_lookup_var).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row3, text="secondary_max").pack(side=tk.LEFT)
        ttk.Entry(row3, textvariable=self.secondary_max_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(row3, text="secondary_top_k").pack(side=tk.LEFT)
        ttk.Entry(row3, textvariable=self.secondary_top_k_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Checkbutton(row3, text="verify_rename", variable=self.verify_rename_var).pack(side=tk.LEFT, padx=(12, 10))
        ttk.Label(row3, text="rename_mode").pack(side=tk.LEFT)
        self.rename_mode_combo = ttk.Combobox(
            row3,
            textvariable=self.verify_rename_mode_var,
            values=rename_mode_labels_for_lang(self.lang_var.get()),
            state="readonly",
            width=20,
        )
        self.rename_mode_combo.pack(side=tk.LEFT, padx=(4, 6))

        actions = ttk.Frame(left)
        actions.pack(fill=tk.X, pady=(8, 0))
        self.run_btn = ttk.Button(actions, text=self._tr("run"), command=self._run)
        self.run_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_btn = ttk.Button(actions, text=self._tr("stop"), command=self._stop)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_btn.state(["disabled"])
        ttk.Button(actions, text=self._tr("load"), command=self._load_config).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text=self._tr("save"), command=self._save_config).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text=self._tr("recommend"), command=self._apply_recommended_preset).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="Edit Generic Sites", command=self._edit_generic_download_sites).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text=self._tr("rename_only"), command=self._rename_only).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text=self._tr("refresh"), command=self._refresh_summary).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text=self._tr("open_output"), command=self._open_output).pack(side=tk.LEFT, padx=(0, 6))

        self.progress = ttk.Progressbar(left, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(8, 0))

        tabs = ttk.Notebook(left)
        tabs.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        log_tab = ttk.Frame(tabs)
        summary_tab = ttk.Frame(tabs)
        tabs.add(log_tab, text=self._tr("logs"))
        tabs.add(summary_tab, text=self._tr("summary"))
        self.log_text = ScrolledText(log_tab, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.log_text.configure(state=tk.DISABLED)
        self.summary_text = ScrolledText(summary_tab, wrap=tk.WORD)
        self.summary_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.summary_text.configure(state=tk.DISABLED)

        ttk.Label(left, textvariable=self.status_var).pack(fill=tk.X, pady=(6, 0))

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf"), ("All files", "*.*")])
        if path:
            self.input_var.set(path)

    def _pick_output(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.output_var.set(path)

    def _pick_config(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.config_var.set(path)

    def _pick_cookies(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Text/JSON", "*.txt *.json"), ("All files", "*.*")])
        if path:
            self.cookies_var.set(path)

    def _pick_cookies_folder(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.cookies_folder_var.set(path)

    def _build_domain_cookies_file(self) -> None:
        try:
            cookies_dir = Path(self.cookies_folder_var.get().strip())
            if not cookies_dir.is_absolute():
                cookies_dir = (self.base_dir / cookies_dir).resolve()
            if not cookies_dir.exists() or not cookies_dir.is_dir():
                raise ValueError(f"cookies folder not found: {cookies_dir}")
            cfg = build_domain_cookies_config_from_folder_with_presets(
                cookies_dir,
                self.custom_cookie_domain_presets,
            )
            output_dir = Path(self.output_var.get().strip() or ".")
            target = Path(self.domain_cookies_file_var.get().strip() or "domain_cookies.json")
            if not target.is_absolute():
                target = (output_dir / target).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_log(
                f"[cookies] built domain cookies config: {target} (domains={len(cfg.get('domains', {}))})\n"
            )
            self.domain_cookies_file_var.set(str(target))
        except Exception as exc:
            messagebox.showerror(self._tr("invalid"), str(exc))

    def _edit_custom_cookie_domains(self) -> None:
        editor = tk.Toplevel(self.root)
        editor.title("Custom Journal -> Domains")
        editor.geometry("720x500")
        editor.transient(self.root)
        editor.grab_set()

        prompt = (
            "JSON object: {\"journal_name\": [\"domain1\", \"domain2\"]}\n"
            "You can also use comma-separated string values.\n"
            "File stem in cookies folder should match journal_name."
        )
        ttk.Label(editor, text=prompt, justify=tk.LEFT).pack(fill=tk.X, padx=10, pady=(10, 6))

        body = ScrolledText(editor, wrap=tk.WORD)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        body.insert(
            "1.0",
            json.dumps(
                self.custom_cookie_domain_presets,
                ensure_ascii=False,
                indent=2,
            ),
        )

        btns = ttk.Frame(editor)
        btns.pack(fill=tk.X, padx=10, pady=(0, 10))

        def on_save() -> None:
            try:
                parsed = json.loads(body.get("1.0", tk.END).strip() or "{}")
                normalized = normalize_cookie_domain_presets(parsed)
                self.custom_cookie_domain_presets = normalized
                self._append_log(
                    f"[cookies] custom journal-domain mappings saved: {len(normalized)} journals\n"
                )
                editor.destroy()
            except Exception as exc:
                messagebox.showerror(self._tr("invalid"), str(exc))

        ttk.Button(btns, text="Save", command=on_save).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Cancel", command=editor.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    def _edit_generic_download_sites(self) -> None:
        editor = tk.Toplevel(self.root)
        editor.title("Generic Download Sites")
        editor.geometry("720x500")
        editor.transient(self.root)
        editor.grab_set()

        prompt = (
            "JSON array of URL templates.\n"
            "Placeholders: {doi}, {doi_encoded}, {title}, {title_encoded}.\n"
            "Examples:\n"
            "  https://sci-hub.se/{doi}\n"
            "  https://example.org/search?q={title_encoded}"
        )
        ttk.Label(editor, text=prompt, justify=tk.LEFT).pack(fill=tk.X, padx=10, pady=(10, 6))

        body = ScrolledText(editor, wrap=tk.WORD)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        initial = self.generic_download_sites or ["https://sci-hub.se/{doi}"]
        body.insert("1.0", json.dumps(initial, ensure_ascii=False, indent=2))

        btns = ttk.Frame(editor)
        btns.pack(fill=tk.X, padx=10, pady=(0, 10))

        def on_save() -> None:
            try:
                parsed = json.loads(body.get("1.0", tk.END).strip() or "[]")
                if not isinstance(parsed, list):
                    raise ValueError("generic_download_sites must be a JSON array")
                cleaned: list[str] = []
                seen: set[str] = set()
                for row in parsed:
                    url = str(row or "").strip()
                    if not url:
                        continue
                    if not url.startswith(("http://", "https://")):
                        raise ValueError(f"invalid URL template: {url}")
                    if url in seen:
                        continue
                    seen.add(url)
                    cleaned.append(url)
                self.generic_download_sites = cleaned
                self._append_log(f"[generic-sites] saved templates: {len(cleaned)}\n")
                editor.destroy()
            except Exception as exc:
                messagebox.showerror(self._tr("invalid"), str(exc))

        ttk.Button(btns, text="Save", command=on_save).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Cancel", command=editor.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    def _collect_config(self) -> dict[str, Any]:
        input_path = Path(self.input_var.get().strip())
        if not input_path.exists():
            raise ValueError("input pdf not found")
        if not input_path.is_file():
            raise ValueError("input path is not a file")
        if input_path.stat().st_size <= 0:
            raise ValueError("input pdf is empty")
        threshold = float(self.verify_threshold_var.get())
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("verify threshold must be in [0,1]")
        return {
            "input": str(input_path),
            "output": self.output_var.get().strip(),
            "cookies": self.cookies_var.get().strip(),
            "domain_cookies_file": self.domain_cookies_file_var.get().strip() or "domain_cookies.json",
            "custom_cookie_domain_presets": self.custom_cookie_domain_presets,
            "generic_download_sites": self.generic_download_sites,
            "pdf_parser": self.pdf_parser_var.get().strip() or "pypdf",
            "workers": int(self.workers_var.get()),
            "timeout": int(self.timeout_var.get()),
            "retries": int(self.retries_var.get()),
            "max_candidates_per_item": int(self.max_candidates_var.get()),
            "secondary_lookup": bool(self.secondary_lookup_var.get()),
            "secondary_max": int(self.secondary_max_var.get()),
            "secondary_top_k": int(self.secondary_top_k_var.get()),
            "verify_title_rename": bool(self.verify_rename_var.get()),
            "verify_rename_mode": self._current_rename_mode_value(),
            "verify_title_threshold": threshold,
            "interactive": "false",
        }

    def _apply_recommended_defaults(self, *, notify_if_fallback: bool) -> None:
        preset = recommended_download_preset()
        parser = str(preset["pdf_parser"])
        if parser == "pdfplumber" and not is_pdfplumber_available():
            parser = "pypdf"
            if notify_if_fallback:
                messagebox.showwarning(
                    self._tr("invalid"),
                    "pdfplumber is not installed. Switched parser to pypdf. "
                    "Install with: pip install pdfplumber",
                )
        self.pdf_parser_var.set(parser)
        self.secondary_lookup_var.set(bool(preset["secondary_lookup"]))
        self.secondary_max_var.set(str(preset["secondary_max"]))
        self.secondary_top_k_var.set(str(preset["secondary_top_k"]))
        self.max_candidates_var.set(str(preset["max_candidates_per_item"]))
        self.retries_var.set(str(preset["retries"]))
        self.timeout_var.set(str(preset["timeout"]))

    def _apply_recommended_preset(self) -> None:
        self._apply_recommended_defaults(notify_if_fallback=True)

    def _run(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        try:
            cfg = self._collect_config()
        except Exception as exc:
            messagebox.showerror(self._tr("invalid"), str(exc))
            return
        if str(cfg.get("pdf_parser", "pypdf")) == "pdfplumber" and not is_pdfplumber_available():
            cfg["pdf_parser"] = "pypdf"
            self.pdf_parser_var.set("pypdf")
            messagebox.showwarning(
                self._tr("invalid"),
                "pdfplumber is not installed. Switched parser to pypdf. "
                "Install with: pip install pdfplumber",
            )
        if not self.script_path.exists():
            messagebox.showerror(self._tr("invalid"), f"missing: {self.script_path}")
            return
        Path(cfg["output"]).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False, dir=self.base_dir) as tmp:
            json.dump(cfg, tmp, ensure_ascii=False, indent=2)
            self.temp_config_path = Path(tmp.name)
        cmd = [sys.executable, str(self.script_path), "--config", str(self.temp_config_path)]
        self._append_log("$ " + " ".join(cmd) + "\n")
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.base_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.run_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.progress.start(10)
        self._set_status("running")
        threading.Thread(target=self._reader_thread, daemon=True).start()

    def _rename_only(self) -> None:
        try:
            if not bool(self.verify_rename_var.get()):
                messagebox.showerror(self._tr("invalid"), "Please enable verify_rename first.")
                return
            out = Path(self.output_var.get().strip() or ".")
            stats = run_rename_only_on_output(
                output_dir=out,
                verify_threshold=float(self.verify_threshold_var.get()),
                rename_mode=self._current_rename_mode_value(),
            )
            self._append_log(
                f"[rename-only] processed={stats['processed']} ok={stats['renamed_ok']} "
                f"mismatch={stats['mismatch']} skipped={stats['skipped']}\n"
            )
            self._refresh_summary()
        except Exception as exc:
            messagebox.showerror(self._tr("invalid"), str(exc))

    def _reader_thread(self) -> None:
        assert self.proc is not None
        if self.proc.stdout is not None:
            for line in self.proc.stdout:
                self.log_queue.put(line)
        self.log_queue.put(f"__EXIT__:{self.proc.wait()}")

    def _stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
        except Exception:
            pass

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _pump_logs(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line.startswith("__EXIT__:"):
                    self._on_finished(int(line.split(":", 1)[1]))
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self.root.after(120, self._pump_logs)

    def _on_finished(self, code: int) -> None:
        self.progress.stop()
        self.run_btn.state(["!disabled"])
        self.stop_btn.state(["disabled"])
        self.proc = None
        self._set_status("finished" if code == 0 else "stopped")
        if self.temp_config_path and self.temp_config_path.exists():
            try:
                self.temp_config_path.unlink()
            except Exception:
                pass
        self.temp_config_path = None
        self._refresh_summary()

    def _save_config(self) -> None:
        try:
            cfg = self._collect_config()
        except Exception as exc:
            messagebox.showerror(self._tr("invalid"), str(exc))
            return
        target = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not target:
            return
        Path(target).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.config_var.set(target)

    def _load_config(self) -> None:
        try:
            path = self.config_var.get().strip()
            if not path:
                path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
                if not path:
                    return
                self.config_var.set(path)
            data = load_gui_config_payload(Path(path))
            self.input_var.set(str(data.get("input", self.input_var.get())))
            self.output_var.set(str(data.get("output", self.output_var.get())))
            self.cookies_var.set(str(data.get("cookies", self.cookies_var.get())))
            self.domain_cookies_file_var.set(str(data.get("domain_cookies_file", self.domain_cookies_file_var.get())))
            self.custom_cookie_domain_presets = normalize_cookie_domain_presets(
                data.get("custom_cookie_domain_presets", self.custom_cookie_domain_presets)
            )
            raw_generic_sites = data.get("generic_download_sites", self.generic_download_sites)
            if isinstance(raw_generic_sites, str):
                raw_generic_sites = [x.strip() for x in raw_generic_sites.split(",")]
            if isinstance(raw_generic_sites, list):
                self.generic_download_sites = [str(x).strip() for x in raw_generic_sites if str(x).strip()]
            self.workers_var.set(str(data.get("workers", self.workers_var.get())))
            self.timeout_var.set(str(data.get("timeout", self.timeout_var.get())))
            self.retries_var.set(str(data.get("retries", self.retries_var.get())))
            self.max_candidates_var.set(str(data.get("max_candidates_per_item", self.max_candidates_var.get())))
            self.pdf_parser_var.set(str(data.get("pdf_parser", self.pdf_parser_var.get())))
            self.secondary_lookup_var.set(bool(data.get("secondary_lookup", self.secondary_lookup_var.get())))
            self.secondary_max_var.set(str(data.get("secondary_max", self.secondary_max_var.get())))
            self.secondary_top_k_var.set(str(data.get("secondary_top_k", self.secondary_top_k_var.get())))
            self.verify_rename_var.set(bool(data.get("verify_title_rename", self.verify_rename_var.get())))
            self._set_rename_mode_from_value(str(data.get("verify_rename_mode", self._current_rename_mode_value())))
            self.verify_threshold_var.set(str(data.get("verify_title_threshold", self.verify_threshold_var.get())))
            self._refresh_summary()
        except Exception as exc:
            messagebox.showerror(self._tr("invalid"), str(exc))

    def _refresh_summary(self) -> None:
        out = Path(self.output_var.get().strip() or ".")
        summary = load_summary_from_output(out)
        lines = [
            f"output: {out}",
            f"total: {summary['total']}",
            f"downloaded_pdf: {summary['downloaded_pdf']}",
            f"saved_landing_url: {summary['saved_landing_url']}",
            f"failed: {summary['failed']}",
            f"not_attempted: {summary['not_attempted']}",
            f"resolved_by_secondary_lookup: {summary['resolved_by_secondary_lookup']}",
        ]
        self.summary_text.configure(state=tk.NORMAL)
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.insert(tk.END, "\n".join(lines) + "\n")
        self.summary_text.configure(state=tk.DISABLED)

    def _open_output(self) -> None:
        target = Path(self.output_var.get().strip() or ".").resolve()
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        else:
            subprocess.run(["xdg-open", str(target)], check=False)

    def _on_close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self._stop()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    ReferenceToolGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
