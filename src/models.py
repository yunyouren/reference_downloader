"""Data models for the reference download tool."""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ReferenceItem:
    """A structured representation of one reference entry.

    - number: 条目编号（数字引用时取原编号；非数字引用时按出现顺序从1开始）
    - text: 清洗后的条目正文
    - dois/urls: 从text中抽取或二次检索得到的DOI/URL候选
    - download_status: 下载状态
    - downloaded_file: 下载到的文件名
    - note: 额外说明
    """

    number: int
    text: str
    dois: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    download_status: str = "not_attempted"
    downloaded_file: str = ""
    note: str = ""


@dataclass
class SecondaryLookupCandidate:
    score: float
    doi: str
    urls: list[str] = field(default_factory=list)


@dataclass
class DownloadAttempt:
    phase: str
    ref_number: int
    candidate_url: str
    final_url: str
    status_code: int
    content_type: str
    outcome: str
    waited_seconds: float
    error: str


class DownloadLogger:
    """Thread-safe download attempt log aggregator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[DownloadAttempt] = []

    def add(self, row: DownloadAttempt) -> None:
        with self._lock:
            self._rows.append(row)

    def write_csv(self, file_path: Path) -> None:
        with self._lock:
            rows = list(self._rows)
        if not rows:
            return
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "phase", "ref_number", "candidate_url", "final_url",
                    "status_code", "content_type", "outcome",
                    "waited_seconds", "error",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))


class SecondaryLookupCache:
    """Thread-safe JSON file cache for secondary lookup results."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8")) or {}
            except Exception:
                self._data = {}

    def get(self, key: str) -> tuple[list[str], list[str]] | None:
        with self._lock:
            row = self._data.get(key)
        if not isinstance(row, dict):
            return None
        dois = row.get("dois")
        urls = row.get("urls")
        if not isinstance(dois, list) or not isinstance(urls, list):
            return None
        if not dois and not urls:
            return None
        return [str(x) for x in dois], [str(x) for x in urls]

    def set(self, key: str, dois: list[str], urls: list[str]) -> None:
        if not dois and not urls:
            return
        with self._lock:
            self._data[key] = {"ts": time.time(), "dois": list(dois), "urls": list(urls)}

    def flush(self) -> None:
        with self._lock:
            data = dict(self._data)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class DomainLimiter:
    """Per-domain concurrency limiter with backoff support."""

    def __init__(self, max_per_domain: int, min_delay_ms: int) -> None:
        self._max_per_domain = max_per_domain
        self._min_delay_s = max(0.0, float(min_delay_ms) / 1000.0)
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._next_allowed: dict[str, float] = {}
        self._backoff_until: dict[str, float] = {}

    def __enter__(self) -> "DomainLimiter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def backoff(self, host: str, seconds: float, now: float | None = None) -> None:
        key = (host or "").lower()
        if not key:
            return
        s = float(seconds)
        if s <= 0:
            return
        t = time.monotonic() if now is None else float(now)
        until = t + s
        with self._lock:
            self._backoff_until[key] = max(self._backoff_until.get(key, 0.0), until)

    def compute_wait_seconds(self, host: str, now: float | None = None) -> float:
        key = (host or "").lower()
        if not key:
            return 0.0
        t = time.monotonic() if now is None else float(now)
        with self._lock:
            next_allowed = self._next_allowed.get(key, 0.0)
            backoff_until = self._backoff_until.get(key, 0.0)
        return max(0.0, max(next_allowed, backoff_until) - t)

    def acquire(self, host: str) -> threading.Semaphore | None:
        key = (host or "").lower()
        if not key:
            return None
        sem: threading.Semaphore | None
        if self._max_per_domain <= 0:
            sem = None
        else:
            with self._lock:
                sem = self._semaphores.get(key)
                if sem is None:
                    sem = threading.Semaphore(self._max_per_domain)
                    self._semaphores[key] = sem
            sem.acquire()
        now = time.monotonic()
        with self._lock:
            next_allowed = self._next_allowed.get(key, 0.0)
            backoff_until = self._backoff_until.get(key, 0.0)
            wait_s = max(0.0, max(next_allowed, backoff_until) - now)
            base = max(next_allowed, backoff_until, now)
            if self._min_delay_s > 0:
                self._next_allowed[key] = base + self._min_delay_s
        if wait_s > 0:
            time.sleep(wait_s)
        return sem

    def release(self, sem: threading.Semaphore | None) -> None:
        if sem is None:
            return
        sem.release()


@dataclass
class PipelineConfig:
    """Configuration for the full reference extraction + download pipeline."""

    input_pdf: Path
    output_dir: Path = field(default_factory=lambda: Path("references_output"))

    # PDF parsing
    pdf_parser: str = "pdfplumber"
    header_margin: float = 40.0
    footer_margin: float = 40.0

    # HTTP / network
    timeout: int = 20
    lookup_timeout: int = 6
    retries: int = 1
    user_agent: str = "ReferenceDownloader/1.1"
    cookies_path: Path | None = None

    # Concurrency
    workers: int = 8
    max_per_domain: int = 2
    min_domain_delay_ms: int = 0
    api_concurrency: int = 1
    api_min_delay_ms: int = 500

    # Download behavior
    download_log: str = "download_log.csv"
    unpaywall_email: str = ""
    max_candidates_per_item: int = 3
    skip_doi: bool = False
    download_max: int = 0
    no_download: bool = False
    resume: bool = True
    show_progress: bool = True

    # Secondary lookup
    secondary_lookup: bool = False
    secondary_max: int = 40
    secondary_top_k: int = 2
    secondary_cache: str = ""
    neurips_proceedings: bool = True

    # Verification
    verify_title_rename: bool = False
    verify_rename_mode: str = "number_and_original"
    verify_title_threshold: float = 0.55

    # Directories
    verified_subdir: str = "verified_pdfs"
    meta_subdir: str = "meta"
    landing_subdir: str = "landing_urls"
    mismatch_subdir: str = "mismatch_pdfs"

    # Site / domain
    generic_download_sites: list[str] = field(default_factory=list)
    domain_cookies_file: str = "domain_cookies.json"
    domain_cookies_config: dict[str, dict] | None = None
    interactive: str = "auto"
