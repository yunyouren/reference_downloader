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
        "language": "语言",
        "input": "输入 PDF",
        "output": "输出目录",
        "config": "配置文件",
        "cookies": "登录凭据文件",
        "cookies_folder": "登录凭据文件夹",
        "domain_cookies_file": "域名凭据配置文件",
        "build": "构建",
        "edit_map": "编辑映射",
        "workers": "线程数",
        "timeout": "超时",
        "retries": "重试",
        "max_candidates": "最大候选数",
        "verify_threshold": "校验阈值",
        "pdf_parser": "PDF 解析器",
        "secondary_lookup": "二次检索",
        "secondary_max": "二次最大数",
        "secondary_top_k": "二次 TopK",
        "verify_rename": "校验并重命名",
        "rename_mode": "重命名模式",
        "parameter_help": "参数说明",
        "run": "运行",
        "stop": "停止",
        "load": "加载配置",
        "save": "保存配置",
        "recommend": "推荐参数",
        "rename_only": "仅重命名",
        "edit_generic_sites": "编辑通用站点",
        "refresh": "刷新统计",
        "open_output": "打开输出目录",
        "logs": "实时日志",
        "summary": "运行统计",
        "running": "运行中...",
        "ready": "就绪",
        "finished": "已完成",
        "stopped": "已停止",
        "invalid": "参数错误",
        "save_btn": "保存",
        "cancel_btn": "取消",
        "custom_map_title": "自定义期刊 -> 域名",
        "custom_map_prompt": "JSON 对象：{\"期刊名\": [\"域名1\", \"域名2\"]}\n也支持逗号分隔字符串值。\nCookies 文件夹中的文件名（不含后缀）需匹配期刊名。",
        "generic_sites_title": "通用下载站点",
        "generic_sites_prompt": "支持占位符：{doi}, {doi_encoded}, {title}, {title_encoded}。\n每行一个模板，编辑后点击保存。\n示例：https://sci-hub.se/{doi} | https://example.org/search?q={title_encoded}",
        "url_template": "URL 模板",
        "add": "添加",
        "add_scihub": "添加 Sci-Hub",
        "add_oa_pack": "添加开放获取站点包",
        "add_title_search": "添加标题检索",
        "delete_selected": "删除选中",
        "move_up": "上移",
        "move_down": "下移",
        "invalid_url_template": "无效 URL 模板",
        "enable_verify_rename_first": "请先启用“校验并重命名”。",
    },
    "en": {
        "title": "Reference Tool GUI",
        "language": "Language",
        "input": "Input PDF",
        "output": "Output Dir",
        "config": "Config",
        "cookies": "Cookies",
        "cookies_folder": "Cookies Folder",
        "domain_cookies_file": "Domain Cookies File",
        "build": "Build",
        "edit_map": "Edit Map",
        "workers": "workers",
        "timeout": "timeout",
        "retries": "retries",
        "max_candidates": "max_candidates",
        "verify_threshold": "verify_threshold",
        "pdf_parser": "pdf_parser",
        "secondary_lookup": "secondary_lookup",
        "secondary_max": "secondary_max",
        "secondary_top_k": "secondary_top_k",
        "verify_rename": "verify_rename",
        "rename_mode": "rename_mode",
        "parameter_help": "Parameter Help",
        "run": "Run",
        "stop": "Stop",
        "load": "Load Config",
        "save": "Save Config",
        "recommend": "Recommended Preset",
        "edit_generic_sites": "Edit Generic Sites",
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
        "save_btn": "Save",
        "cancel_btn": "Cancel",
        "custom_map_title": "Custom Journal -> Domains",
        "custom_map_prompt": "JSON object: {\"journal_name\": [\"domain1\", \"domain2\"]}\nYou can also use comma-separated string values.\nFile stem in cookies folder should match journal_name.",
        "generic_sites_title": "Generic Download Sites",
        "generic_sites_prompt": "Placeholders: {doi}, {doi_encoded}, {title}, {title_encoded}.\nAdd one template per row, then Save.\nExamples: https://sci-hub.se/{doi} | https://example.org/search?q={title_encoded}",
        "url_template": "URL Template",
        "add": "Add",
        "add_scihub": "Add Sci-Hub",
        "add_oa_pack": "Add OA Pack",
        "add_title_search": "Add Title Search",
        "delete_selected": "Delete Selected",
        "move_up": "Move Up",
        "move_down": "Move Down",
        "invalid_url_template": "invalid URL template",
        "enable_verify_rename_first": "Please enable verify_rename first.",
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


DEFAULT_GENERIC_DOWNLOAD_SITES: list[str] = [
    "https://arxiv.org/search/?query={title_encoded}&searchtype=all",
    "https://www.semanticscholar.org/search?q={title_encoded}",
    "https://core.ac.uk/search?q=doi:{doi_encoded}",
    "https://www.base-search.net/Search/Results?lookfor={title_encoded}&type=all&oaboost=1",
    "https://doaj.org/search/articles/{title_encoded}",
]


def recommended_download_preset() -> dict[str, Any]:
    return {
        "pdf_parser": "pdfplumber",
        "secondary_lookup": True,
        "secondary_max": 60,
        "secondary_top_k": 3,
        "max_candidates_per_item": 5,
        "retries": 2,
        "timeout": 25,
        "generic_download_sites": list(DEFAULT_GENERIC_DOWNLOAD_SITES),
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
        self._refresh_static_texts()
        self._refresh_rename_mode_labels()
        self._refresh_help_text()

    def _refresh_static_texts(self) -> None:
        self.root.title(self._tr("title"))
        if hasattr(self, "lang_label"):
            self.lang_label.configure(text=self._tr("language"))
        if hasattr(self, "help_frame"):
            self.help_frame.configure(text=self._tr("parameter_help"))
        if hasattr(self, "lbl_input"):
            self.lbl_input.configure(text=self._tr("input"))
        if hasattr(self, "lbl_output"):
            self.lbl_output.configure(text=self._tr("output"))
        if hasattr(self, "lbl_config"):
            self.lbl_config.configure(text=self._tr("config"))
        if hasattr(self, "lbl_cookies"):
            self.lbl_cookies.configure(text=self._tr("cookies"))
        if hasattr(self, "lbl_cookies_folder"):
            self.lbl_cookies_folder.configure(text=self._tr("cookies_folder"))
        if hasattr(self, "lbl_domain_cookies_file"):
            self.lbl_domain_cookies_file.configure(text=self._tr("domain_cookies_file"))
        if hasattr(self, "btn_build_domain"):
            self.btn_build_domain.configure(text=self._tr("build"))
        if hasattr(self, "btn_edit_map"):
            self.btn_edit_map.configure(text=self._tr("edit_map"))
        if hasattr(self, "lbl_workers"):
            self.lbl_workers.configure(text=self._tr("workers"))
        if hasattr(self, "lbl_timeout"):
            self.lbl_timeout.configure(text=self._tr("timeout"))
        if hasattr(self, "lbl_retries"):
            self.lbl_retries.configure(text=self._tr("retries"))
        if hasattr(self, "lbl_max_candidates"):
            self.lbl_max_candidates.configure(text=self._tr("max_candidates"))
        if hasattr(self, "lbl_verify_threshold"):
            self.lbl_verify_threshold.configure(text=self._tr("verify_threshold"))
        if hasattr(self, "lbl_pdf_parser"):
            self.lbl_pdf_parser.configure(text=self._tr("pdf_parser"))
        if hasattr(self, "chk_secondary_lookup"):
            self.chk_secondary_lookup.configure(text=self._tr("secondary_lookup"))
        if hasattr(self, "lbl_secondary_max"):
            self.lbl_secondary_max.configure(text=self._tr("secondary_max"))
        if hasattr(self, "lbl_secondary_top_k"):
            self.lbl_secondary_top_k.configure(text=self._tr("secondary_top_k"))
        if hasattr(self, "chk_verify_rename"):
            self.chk_verify_rename.configure(text=self._tr("verify_rename"))
        if hasattr(self, "lbl_rename_mode"):
            self.lbl_rename_mode.configure(text=self._tr("rename_mode"))
        if hasattr(self, "run_btn"):
            self.run_btn.configure(text=self._tr("run"))
        if hasattr(self, "stop_btn"):
            self.stop_btn.configure(text=self._tr("stop"))
        if hasattr(self, "load_btn"):
            self.load_btn.configure(text=self._tr("load"))
        if hasattr(self, "save_btn"):
            self.save_btn.configure(text=self._tr("save"))
        if hasattr(self, "recommend_btn"):
            self.recommend_btn.configure(text=self._tr("recommend"))
        if hasattr(self, "edit_generic_sites_btn"):
            self.edit_generic_sites_btn.configure(text=self._tr("edit_generic_sites"))
        if hasattr(self, "rename_only_btn"):
            self.rename_only_btn.configure(text=self._tr("rename_only"))
        if hasattr(self, "refresh_btn"):
            self.refresh_btn.configure(text=self._tr("refresh"))
        if hasattr(self, "open_output_btn"):
            self.open_output_btn.configure(text=self._tr("open_output"))
        if hasattr(self, "tabs"):
            self.tabs.tab(0, text=self._tr("logs"))
            self.tabs.tab(1, text=self._tr("summary"))
        if hasattr(self, "status_var") and self.status_var.get():
            # keep semantic status key behavior from _set_status callers
            pass

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
        self.lang_label = ttk.Label(lang_row, text=self._tr("language"))
        self.lang_label.pack(side=tk.LEFT)
        ttk.Combobox(lang_row, textvariable=self.lang_var, values=["en", "zh"], state="readonly", width=8).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.lang_var.trace_add("write", self._on_lang_change)

        top = ttk.Frame(frame)
        top.pack(fill=tk.BOTH, expand=False)

        left = ttk.Frame(top)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.help_frame = ttk.LabelFrame(top, text=self._tr("parameter_help"))
        self.help_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))

        self.help_text = ScrolledText(self.help_frame, width=44, height=26, wrap=tk.WORD)
        self.help_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._refresh_help_text()

        settings = ttk.Frame(left)
        settings.pack(fill=tk.X)
        self.lbl_input = ttk.Label(settings, text=self._tr("input"))
        self.lbl_input.grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.input_var, width=92).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_input).grid(row=0, column=2)
        self.lbl_output = ttk.Label(settings, text=self._tr("output"))
        self.lbl_output.grid(row=1, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.output_var, width=92).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_output).grid(row=1, column=2)
        self.lbl_config = ttk.Label(settings, text=self._tr("config"))
        self.lbl_config.grid(row=2, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.config_var, width=92).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_config).grid(row=2, column=2)
        self.lbl_cookies = ttk.Label(settings, text=self._tr("cookies"))
        self.lbl_cookies.grid(row=3, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.cookies_var, width=92).grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_cookies).grid(row=3, column=2)
        self.lbl_cookies_folder = ttk.Label(settings, text=self._tr("cookies_folder"))
        self.lbl_cookies_folder.grid(row=4, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.cookies_folder_var, width=92).grid(row=4, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="...", width=4, command=self._pick_cookies_folder).grid(row=4, column=2)
        self.lbl_domain_cookies_file = ttk.Label(settings, text=self._tr("domain_cookies_file"))
        self.lbl_domain_cookies_file.grid(row=5, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.domain_cookies_file_var, width=92).grid(row=5, column=1, sticky="ew", padx=6)
        domain_actions = ttk.Frame(settings)
        domain_actions.grid(row=5, column=2, sticky="e")
        self.btn_build_domain = ttk.Button(domain_actions, text=self._tr("build"), width=6, command=self._build_domain_cookies_file)
        self.btn_build_domain.pack(side=tk.LEFT)
        self.btn_edit_map = ttk.Button(domain_actions, text=self._tr("edit_map"), width=10, command=self._edit_custom_cookie_domains)
        self.btn_edit_map.pack(side=tk.LEFT, padx=(4, 0))
        settings.grid_columnconfigure(1, weight=1)

        row2 = ttk.Frame(left)
        row2.pack(fill=tk.X, pady=(8, 0))
        self.lbl_workers = ttk.Label(row2, text=self._tr("workers"))
        self.lbl_workers.pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.workers_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        self.lbl_timeout = ttk.Label(row2, text=self._tr("timeout"))
        self.lbl_timeout.pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.timeout_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        self.lbl_retries = ttk.Label(row2, text=self._tr("retries"))
        self.lbl_retries.pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.retries_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        self.lbl_max_candidates = ttk.Label(row2, text=self._tr("max_candidates"))
        self.lbl_max_candidates.pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.max_candidates_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        self.lbl_verify_threshold = ttk.Label(row2, text=self._tr("verify_threshold"))
        self.lbl_verify_threshold.pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.verify_threshold_var, width=8).pack(side=tk.LEFT, padx=(4, 10))

        row3 = ttk.Frame(left)
        row3.pack(fill=tk.X, pady=(6, 0))
        self.lbl_pdf_parser = ttk.Label(row3, text=self._tr("pdf_parser"))
        self.lbl_pdf_parser.pack(side=tk.LEFT)
        ttk.Combobox(
            row3,
            textvariable=self.pdf_parser_var,
            values=["pypdf", "pdfplumber"],
            state="readonly",
            width=14,
        ).pack(side=tk.LEFT, padx=(4, 10))
        self.chk_secondary_lookup = ttk.Checkbutton(row3, text=self._tr("secondary_lookup"), variable=self.secondary_lookup_var)
        self.chk_secondary_lookup.pack(side=tk.LEFT, padx=(4, 10))
        self.lbl_secondary_max = ttk.Label(row3, text=self._tr("secondary_max"))
        self.lbl_secondary_max.pack(side=tk.LEFT)
        ttk.Entry(row3, textvariable=self.secondary_max_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        self.lbl_secondary_top_k = ttk.Label(row3, text=self._tr("secondary_top_k"))
        self.lbl_secondary_top_k.pack(side=tk.LEFT)
        ttk.Entry(row3, textvariable=self.secondary_top_k_var, width=8).pack(side=tk.LEFT, padx=(4, 10))
        self.chk_verify_rename = ttk.Checkbutton(row3, text=self._tr("verify_rename"), variable=self.verify_rename_var)
        self.chk_verify_rename.pack(side=tk.LEFT, padx=(12, 10))
        self.lbl_rename_mode = ttk.Label(row3, text=self._tr("rename_mode"))
        self.lbl_rename_mode.pack(side=tk.LEFT)
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
        self.load_btn = ttk.Button(actions, text=self._tr("load"), command=self._load_config)
        self.load_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.save_btn = ttk.Button(actions, text=self._tr("save"), command=self._save_config)
        self.save_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.recommend_btn = ttk.Button(actions, text=self._tr("recommend"), command=self._apply_recommended_preset)
        self.recommend_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.edit_generic_sites_btn = ttk.Button(actions, text=self._tr("edit_generic_sites"), command=self._edit_generic_download_sites)
        self.edit_generic_sites_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.rename_only_btn = ttk.Button(actions, text=self._tr("rename_only"), command=self._rename_only)
        self.rename_only_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.refresh_btn = ttk.Button(actions, text=self._tr("refresh"), command=self._refresh_summary)
        self.refresh_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.open_output_btn = ttk.Button(actions, text=self._tr("open_output"), command=self._open_output)
        self.open_output_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.progress = ttk.Progressbar(left, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(8, 0))

        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        log_tab = ttk.Frame(self.tabs)
        summary_tab = ttk.Frame(self.tabs)
        self.tabs.add(log_tab, text=self._tr("logs"))
        self.tabs.add(summary_tab, text=self._tr("summary"))
        self.log_text = ScrolledText(log_tab, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.log_text.configure(state=tk.DISABLED)
        self.summary_text = ScrolledText(summary_tab, wrap=tk.WORD)
        self.summary_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.summary_text.configure(state=tk.DISABLED)

        ttk.Label(left, textvariable=self.status_var).pack(fill=tk.X, pady=(6, 0))
        self._refresh_static_texts()

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
        editor.title(self._tr("custom_map_title"))
        editor.geometry("720x500")
        editor.transient(self.root)
        editor.grab_set()

        prompt = self._tr("custom_map_prompt")
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

        ttk.Button(btns, text=self._tr("save_btn"), command=on_save).pack(side=tk.RIGHT)
        ttk.Button(btns, text=self._tr("cancel_btn"), command=editor.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    def _edit_generic_download_sites(self) -> None:
        editor = tk.Toplevel(self.root)
        editor.title(self._tr("generic_sites_title"))
        editor.geometry("860x560")
        editor.transient(self.root)
        editor.grab_set()

        prompt = self._tr("generic_sites_prompt")
        ttk.Label(editor, text=prompt, justify=tk.LEFT).pack(fill=tk.X, padx=10, pady=(10, 6))

        table_frame = ttk.Frame(editor)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        tree = ttk.Treeview(table_frame, columns=("template",), show="headings", height=12)
        tree.heading("template", text=self._tr("url_template"))
        tree.column("template", width=740, anchor="w")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        for url in (self.generic_download_sites or list(DEFAULT_GENERIC_DOWNLOAD_SITES)):
            tree.insert("", tk.END, values=(url,))

        edit_row = ttk.Frame(editor)
        edit_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        new_template_var = tk.StringVar(value="")
        ttk.Entry(edit_row, textvariable=new_template_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

        def add_row(template: str) -> None:
            value = str(template or "").strip()
            if not value:
                return
            tree.insert("", tk.END, values=(value,))
            new_template_var.set("")

        def add_oa_pack() -> None:
            existing = {
                str(tree.item(item_id, "values")[0]).strip()
                for item_id in tree.get_children("")
                if tree.item(item_id, "values")
            }
            for site in DEFAULT_GENERIC_DOWNLOAD_SITES:
                if site not in existing:
                    tree.insert("", tk.END, values=(site,))
                    existing.add(site)

        ttk.Button(edit_row, text=self._tr("add"), command=lambda: add_row(new_template_var.get())).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(edit_row, text=self._tr("add_scihub"), command=lambda: add_row("https://sci-hub.se/{doi}")).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(edit_row, text=self._tr("add_oa_pack"), command=add_oa_pack).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(
            edit_row,
            text=self._tr("add_title_search"),
            command=lambda: add_row("https://example.org/search?q={title_encoded}"),
        ).pack(side=tk.LEFT, padx=(6, 0))

        ops_row = ttk.Frame(editor)
        ops_row.pack(fill=tk.X, padx=10, pady=(0, 8))

        def delete_selected() -> None:
            selected = tree.selection()
            for item_id in selected:
                tree.delete(item_id)

        def move_selected(delta: int) -> None:
            selected = list(tree.selection())
            if not selected:
                return
            children = list(tree.get_children(""))
            if delta < 0:
                selected.sort(key=lambda i: children.index(i))
            else:
                selected.sort(key=lambda i: children.index(i), reverse=True)
            for item_id in selected:
                current_children = list(tree.get_children(""))
                idx = current_children.index(item_id)
                target = idx + delta
                if target < 0 or target >= len(current_children):
                    continue
                tree.move(item_id, "", target)

        ttk.Button(ops_row, text=self._tr("delete_selected"), command=delete_selected).pack(side=tk.LEFT)
        ttk.Button(ops_row, text=self._tr("move_up"), command=lambda: move_selected(-1)).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(ops_row, text=self._tr("move_down"), command=lambda: move_selected(1)).pack(side=tk.LEFT, padx=(6, 0))

        btns = ttk.Frame(editor)
        btns.pack(fill=tk.X, padx=10, pady=(0, 10))

        def on_save() -> None:
            try:
                cleaned: list[str] = []
                seen: set[str] = set()
                for item_id in tree.get_children(""):
                    values = tree.item(item_id, "values")
                    url = str(values[0] if values else "").strip()
                    if not url:
                        continue
                    if not url.startswith(("http://", "https://")):
                        raise ValueError(f"{self._tr('invalid_url_template')}: {url}")
                    if url in seen:
                        continue
                    seen.add(url)
                    cleaned.append(url)
                self.generic_download_sites = cleaned
                self._append_log(f"[generic-sites] saved templates: {len(cleaned)}\n")
                editor.destroy()
            except Exception as exc:
                messagebox.showerror(self._tr("invalid"), str(exc))

        ttk.Button(btns, text=self._tr("save_btn"), command=on_save).pack(side=tk.RIGHT)
        ttk.Button(btns, text=self._tr("cancel_btn"), command=editor.destroy).pack(side=tk.RIGHT, padx=(0, 6))

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
        generic_sites = preset.get("generic_download_sites", [])
        if isinstance(generic_sites, list):
            self.generic_download_sites = [str(x).strip() for x in generic_sites if str(x).strip()]

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
                messagebox.showerror(self._tr("invalid"), self._tr("enable_verify_rename_first"))
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
