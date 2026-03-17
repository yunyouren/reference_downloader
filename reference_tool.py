#!/usr/bin/env python3
"""
Extract, number, and download references from a paper PDF.

中文说明（概览）
----------------
这个脚本用于从论文 PDF 中：
1) 提取"参考文献/References"章节文本；
2) 将每条参考文献进行分段与编号；
3) 从条目文本中提取 DOI/URL；
4) 尝试自动下载 PDF（或保存落地页链接）；
5) 生成结构化输出（Markdown / CSV / JSON）。

整体流程（main）
--------------
读取 PDF 文本 -> 定位参考文献章节 -> 解析条目 -> 初次下载（可选）
-> 失败项二次检索（可选，Crossref/OpenAlex）-> 写入输出文件。

Features:
- Multiple parsing modes: numeric [1], 1., (1), and author/year-like entries (APA/MLA heuristics)
- Concurrent download with requests.Session() connection reuse
- Optional progress bars via tqdm
- Optional pdfplumber parser to reduce header/footer interference
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
import threading
import time
import hashlib
import xml.etree.ElementTree as ET
from http.cookiejar import Cookie, MozillaCookieJar
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse

import requests  # type: ignore[import-untyped]
from requests.adapters import HTTPAdapter  # type: ignore[import-untyped]
import site_handlers
from core.http import is_probably_pdf, parse_retry_after_seconds, should_record_landing_url
from core.html import extract_springer_pdf_url, extract_ieee_arnumber, extract_ieee_pdf_url
from core.urls import normalize_candidate_url
from core.verify import (
    build_verified_pdf_name,
    VerifyWeights,
    extract_pdf_best_line_score,
    extract_pdf_first_page_text,
    extract_pdf_title_from_file,
    move_verified_pdf,
    sanitize_filename_component,
    title_match_score,
    unique_path,
    verify_and_rename_pdf,
)

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    from PyPDF2 import PdfReader  # type: ignore

try:  # Optional dependency
    import pdfplumber  # type: ignore[import-not-found,import-untyped]
except ImportError:  # pragma: no cover
    pdfplumber = None  # type: ignore

try:  # Optional dependency
    from tqdm import tqdm  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore


DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\]]+", re.IGNORECASE)

# 数字编号条目提取：匹配 [1] / 1. / 1) / (1) / （1） 等起始编号，并捕获条目正文。
NUMERIC_REF_RE = re.compile(
    r"(?ms)^\s*(?:\[(\d+)\]|(\d+)[\.\)]|[\(（](\d+)[\)）])\s+(.*?)(?=^\s*(?:\[\d+\]|\d+[\.\)]|[\(（]\d+[\)）])\s+|\Z)"
)

# 参考文献章节标题（中英文常见写法）。
REF_HEADING_RE = re.compile(
    r"(?im)^\s*(references|bibliography|works cited|reference list|参考文献)\s*$"
)
# 参考文献章节结束位置的启发式检测：常见落在附录、致谢、作者介绍等之前。
REF_END_RE = re.compile(
    r"(?im)^\s*(appendix|appendices|acknowledg(e)?ments?|about the authors?)\b"
)
# 非数字编号分段时，用于判断"这一行很可能是一个新条目的开头"（APA 风格常见）。
AUTHOR_YEAR_START_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\- ]{0,40},\s+.+\((?:19|20)\d{2}[a-z]?\)"
)
# 非数字编号分段时，用于判断"这一行很可能是一个新条目的开头"（MLA 风格启发式）。
MLA_LIKE_START_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\- ]{0,40},\s+.+\.\s+.+"
)


@dataclass
class ReferenceItem:
    """
    一条参考文献的结构化表示。

    - number: 条目编号（数字引用时取原编号；非数字引用时按出现顺序从 1 开始）
    - text: 清洗后的条目正文（尽量合并断行、去掉多余空白）
    - dois/urls: 从 text 中抽取或二次检索得到的 DOI/URL 候选
    - download_status: 下载状态（not_attempted / downloaded_pdf / saved_landing_url / failed）
    - downloaded_file: 下载到的文件名（例如 001.pdf 或 001_landing.url.txt）
    - note: 额外说明（例如最终跳转 URL、失败原因、二次检索标记等）
    """

    number: int
    text: str
    dois: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    download_status: str = "not_attempted"
    downloaded_file: str = ""
    note: str = ""


def apply_resume_state(refs: list[ReferenceItem], output_dir: Path, downloads_dir: Path) -> None:
    state_file = output_dir / "references.json"
    if not state_file.exists():
        return
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, list):
        return
    by_num = {r.number: r for r in refs}
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            num = int(row.get("number"))
        except Exception:
            continue
        item = by_num.get(num)
        if item is None:
            continue
        status = str(row.get("download_status") or "")
        downloaded_file = str(row.get("downloaded_file") or "")
        note = str(row.get("note") or "")
        if status not in {"downloaded_pdf", "saved_landing_url"}:
            continue
        p = downloads_dir / downloaded_file if downloaded_file else None
        if p is not None and p.exists():
            item.download_status = status
            item.downloaded_file = downloaded_file
            item.note = note
            continue
        if status == "downloaded_pdf":
            prefix = f"{num:03d}"
            matches = list(downloads_dir.rglob(f"{prefix}*.pdf"))
            matches = [m for m in matches if m.is_file()]
            matches = [m for m in matches if "__mismatch" not in m.name]
            if len(matches) == 1:
                rel = matches[0].relative_to(downloads_dir).as_posix()
                item.download_status = status
                item.downloaded_file = rel
                item.note = note


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
                    "phase",
                    "ref_number",
                    "candidate_url",
                    "final_url",
                    "status_code",
                    "content_type",
                    "outcome",
                    "waited_seconds",
                    "error",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))


class SecondaryLookupCache:
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
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class DomainLimiter:
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


def make_session(pool_size: int, user_agent: str, cookies_jar: MozillaCookieJar | None) -> requests.Session:
    """
    创建带连接池的 requests.Session，用于多线程下载时复用连接。

    - pool_size: HTTPAdapter 的连接池大小（连接数上限）
    - user_agent: 便于对端识别的 UA（避免一些站点拦截默认 UA）
    """
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": user_agent})
    if cookies_jar is not None:
        for cookie in cookies_jar:
            session.cookies.set_cookie(cookie)
    return session


def load_cookies_txt(path: Path) -> MozillaCookieJar:
    def cookie_from_json(row: dict) -> Cookie | None:
        name = row.get("name")
        value = row.get("value")
        domain = row.get("domain")
        if not isinstance(name, str) or not isinstance(value, str) or not isinstance(domain, str):
            return None
        if value == "_remove_" or not value:
            return None
        path_value = row.get("path") if isinstance(row.get("path"), str) else "/"
        host_only = bool(row.get("hostOnly", False))
        domain_initial_dot = domain.startswith(".")
        domain_specified = not host_only
        secure = bool(row.get("secure", False))
        session_cookie = bool(row.get("session", False))
        expires: int | None = None
        if not session_cookie:
            exp = row.get("expirationDate")
            if isinstance(exp, (int, float)):
                expires = int(exp)
        rest: dict[str, object] = {}
        if bool(row.get("httpOnly", False)):
            rest["HttpOnly"] = None
        return Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain.lstrip(".") if host_only else domain,
            domain_specified=domain_specified,
            domain_initial_dot=domain_initial_dot and not host_only,
            path=path_value,
            path_specified=True,
            secure=secure,
            expires=expires,
            discard=session_cookie,
            comment=None,
            comment_url=None,
            rest=rest,
            rfc2109=False,
        )

    raw = path.read_text(encoding="utf-8", errors="ignore")
    bracket = raw.find("[")
    if bracket != -1:
        try:
            data = json.loads(raw[bracket:])
            if isinstance(data, list):
                jar = MozillaCookieJar()
                for row in data:
                    if isinstance(row, dict):
                        cookie = cookie_from_json(row)
                        if cookie is not None:
                            jar.set_cookie(cookie)
                if len(jar) > 0:
                    return jar
        except Exception:
            pass

    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def is_probably_pdf(first_bytes: bytes) -> bool:
    sniff = first_bytes[:1024].lstrip()
    return sniff.startswith(b"%PDF-")


def parse_retry_after_seconds(value: str) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        if math.isfinite(seconds):
            return max(0.0, seconds)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        now = datetime.now(dt.tzinfo)
        seconds = (dt - now).total_seconds()
        return max(0.0, seconds)
    except Exception:
        return None


def load_config_file(path: Path) -> dict:
    def strip_jsonc(text: str) -> str:
        out: list[str] = []
        i = 0
        in_string = False
        escape = False
        while i < len(text):
            ch = text[i]
            if in_string:
                out.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                i += 1
                continue

            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue

            if ch == "/" and i + 1 < len(text):
                nxt = text[i + 1]
                if nxt == "/":
                    i += 2
                    while i < len(text) and text[i] not in "\r\n":
                        i += 1
                    continue
                if nxt == "*":
                    i += 2
                    while i + 1 < len(text):
                        if text[i] == "*" and text[i + 1] == "/":
                            i += 2
                            break
                        i += 1
                    continue

            out.append(ch)
            i += 1
        return "".join(out)

    def strip_trailing_commas(text: str) -> str:
        out: list[str] = []
        i = 0
        in_string = False
        escape = False
        while i < len(text):
            ch = text[i]
            if in_string:
                out.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                i += 1
                continue

            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue

            if ch == ",":
                j = i + 1
                while j < len(text) and text[j].isspace():
                    j += 1
                if j < len(text) and text[j] in "}]":
                    i += 1
                    continue
                out.append(ch)
                i += 1
                continue

            out.append(ch)
            i += 1
        return "".join(out)

    raw = path.read_text(encoding="utf-8-sig")
    normalized = strip_trailing_commas(strip_jsonc(raw))
    data = json.loads(normalized)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def read_pdf_text_pypdf(pdf_path: Path) -> str:
    """使用 pypdf/PyPDF2 提取 PDF 全文文本（简单直观，但易混入页眉页脚）。"""
    reader = PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def read_pdf_text_pdfplumber(pdf_path: Path, header_margin: float, footer_margin: float) -> str:
    """
    使用 pdfplumber 提取 PDF 文本，并通过裁剪 bbox 尽量避开页眉页脚。

    header_margin/footer_margin 为裁剪边距（单位：PDF 坐标点）。
    """
    if pdfplumber is None:
        raise RuntimeError("pdfplumber not installed. Run: pip install pdfplumber")
    pages: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            bbox = (0, header_margin, page.width, max(header_margin + 1, page.height - footer_margin))
            text = (page.within_bbox(bbox).extract_text() or "").strip()
            if not text:
                text = (page.extract_text() or "").strip()
            pages.append(text)
    return "\n".join(pages)


def read_pdf_text(
    pdf_path: Path,
    parser: str,
    header_margin: float,
    footer_margin: float,
) -> str:
    """根据命令行参数选择 PDF 文本提取后端。"""
    if parser == "pdfplumber":
        if pdfplumber is None:
            print("pdfplumber is not installed; falling back to pypdf.", file=sys.stderr)
            return read_pdf_text_pypdf(pdf_path)
        return read_pdf_text_pdfplumber(pdf_path, header_margin=header_margin, footer_margin=footer_margin)
    return read_pdf_text_pypdf(pdf_path)


def cleanup_reference_text(text: str) -> str:
    """
    清洗参考文献条目文本。

    目的：
    - 将常见的"智能引号"转为普通引号，便于后续正则处理与输出一致性；
    - 处理 PDF 复制文本中常见的断词/断行（例如行尾连字符 + 换行）；
    - 将多行压成一行，规整空白字符。
    """
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_references_section(full_text: str) -> str:
    """
    从全文文本中截取"参考文献/References"章节。

    实现思路：
    - 先用 REF_HEADING_RE 找到可能的标题行；
    - 为避免误匹配目录或前文提到的 "References"，优先选择位于全文后 70% 区域的标题；
    - 再用 REF_END_RE 启发式找一个结束点（附录/致谢等）。
    """
    heading_matches = list(REF_HEADING_RE.finditer(full_text))
    if not heading_matches:
        raise ValueError("Could not find references heading in PDF text.")

    # Prefer headings in later part of document.
    threshold = int(len(full_text) * 0.3)
    start_match = next((m for m in heading_matches if m.start() >= threshold), heading_matches[-1])
    start = start_match.end()
    tail = full_text[start:]
    end_match = REF_END_RE.search(tail)
    if end_match:
        return tail[: end_match.start()]
    return tail


def parse_numeric_references(ref_section_text: str) -> list[ReferenceItem]:
    """
    解析"数字编号"风格的参考文献。

    特点：
    - 条目以编号开头（[1] / 1. / (1) 等）
    - 使用 NUMERIC_REF_RE 进行跨行匹配，提取每个条目块
    """
    refs: list[ReferenceItem] = []
    for match in NUMERIC_REF_RE.finditer(ref_section_text):
        number = int(match.group(1) or match.group(2) or match.group(3))
        raw = cleanup_reference_text(match.group(4))
        if not raw:
            continue
        dois = sorted({d.rstrip(".,;") for d in DOI_RE.findall(raw)})
        urls = sorted({u.rstrip(".,;") for u in URL_RE.findall(raw)})
        refs.append(ReferenceItem(number=number, text=raw, dois=dois, urls=urls))
    return refs


def is_reference_start_line(line: str) -> bool:
    """
    判断一行文本是否像"非数字编号"参考文献条目的起始行。

    这是启发式规则：宁可略微误判，也要在常见 APA/MLA 样式下能较好分段。
    """
    if AUTHOR_YEAR_START_RE.match(line):
        return True
    if MLA_LIKE_START_RE.match(line):
        return True
    # Another common pattern: "Author et al., 2021, ..."
    if re.match(r"^[A-Z].+et al\.,\s*(?:19|20)\d{2}", line):
        return True
    return False


def parse_non_numeric_references(ref_section_text: str) -> list[ReferenceItem]:
    """
    解析"非数字编号"风格的参考文献（例如 APA/MLA）。

    处理步骤：
    - 按行拆分并清理空行/纯页码行；
    - 用 is_reference_start_line() 判断何时开始新条目；
    - 如果分段效果很差，再退化为按空行块切分。
    """
    lines = [ln.strip() for ln in ref_section_text.splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"\d{1,3}", ln)]

    refs_text: list[str] = []
    current: list[str] = []
    for line in lines:
        if is_reference_start_line(line) and current:
            refs_text.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        refs_text.append(" ".join(current))

    # Fallback: if still poor segmentation, split by blank-line blocks.
    if len(refs_text) <= 1:
        blocks = [b.strip() for b in re.split(r"\n\s*\n", ref_section_text) if b.strip()]
        if blocks:
            refs_text = blocks

    refs: list[ReferenceItem] = []
    for idx, raw_text in enumerate(refs_text, start=1):
        raw = cleanup_reference_text(raw_text)
        if len(raw) < 20:
            continue
        dois = sorted({d.rstrip(".,;") for d in DOI_RE.findall(raw)})
        urls = sorted({u.rstrip(".,;") for u in URL_RE.findall(raw)})
        refs.append(ReferenceItem(number=idx, text=raw, dois=dois, urls=urls))
    return refs


def split_references(ref_section_text: str) -> list[ReferenceItem]:
    """
    统一入口：自动选择更合适的参考文献解析策略。

    规则：
    - 先尝试数字编号解析；如果能解析出 >=3 条，基本可判定为数字风格；
    - 否则尝试非数字风格解析；
    - 两者都失败则抛出异常。
    """
    numeric = parse_numeric_references(ref_section_text)
    if len(numeric) >= 3:
        return numeric
    non_numeric = parse_non_numeric_references(ref_section_text)
    if non_numeric:
        return non_numeric
    raise ValueError("Unable to parse references from section.")


def guess_title_query(ref_text: str) -> str:
    """
    从参考文献条目中猜测一个"更像标题"的查询字符串，用于二次检索。

    做法：
    - 去掉引号等符号；
    - 按常见分隔符切成片段（.;。；），取最长片段作为候选；
    - 尝试剔除 vol/no/pp 等尾部信息，避免检索噪声；
    - 最终限制长度，避免请求参数过长。
    """
    m = re.findall(r"[\"""]([^\"""]{8,300})[\"""]", ref_text)
    if m:
        best = max((s.strip() for s in m), key=len, default="")
        if best:
            return best[:180].strip()

    tmp = re.sub(r"['\"""‘’]", "", ref_text)
    parts = [p.strip() for p in re.split(r"[.;。；]", tmp) if p.strip()]
    if not parts:
        return ref_text[:120]
    best = max(parts, key=len)
    best = re.sub(r"\b(?:vol|no|pp|ed|dept|univ|university)\b.*$", "", best, flags=re.IGNORECASE)
    return best[:180].strip()


def parse_ref_year(ref_text: str) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", ref_text)
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def parse_first_author_surname(ref_text: str) -> str:
    txt = (ref_text or "").strip()
    if not txt:
        return ""
    m = re.match(r"^\s*([A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\\-]{1,40})\b", txt)
    if m:
        return m.group(1).lower()
    m2 = re.search(r"\b([A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\\-]{1,40})\s*,", txt)
    if m2:
        return m2.group(1).lower()
    return ""


def secondary_title_score(candidate_title: str, expected_title: str) -> float:
    def tokens(text: str) -> set[str]:
        raw = (text or "").lower()
        raw = re.sub(r"[\u2010-\u2015\u2212]", "-", raw)
        raw = re.sub(r"[^a-z0-9]+", " ", raw)
        toks = [t for t in raw.split() if len(t) >= 3]
        stop = {
            "the",
            "and",
            "for",
            "with",
            "from",
            "into",
            "over",
            "under",
            "between",
            "within",
            "using",
            "use",
            "via",
            "based",
            "model",
            "models",
            "analysis",
            "study",
            "method",
            "methods",
            "approach",
            "approaches",
            "system",
            "systems",
            "paper",
            "review",
        }
        return {t for t in toks if t not in stop}

    a = tokens(candidate_title)
    b = tokens(expected_title)
    if not a or not b:
        return 0.0
    return float(len(a & b)) / float(len(a | b))


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


@dataclass
class SecondaryLookupCandidate:
    score: float
    doi: str
    urls: list[str] = field(default_factory=list)


def lookup_secondary_ranked(
    session: requests.Session,
    item: ReferenceItem,
    timeout: int,
    top_k: int,
    api_limiter: DomainLimiter | None = None,
) -> tuple[list[str], list[str]]:
    expected = guess_title_query(item.text)
    ref_year = parse_ref_year(item.text)
    surname = parse_first_author_surname(item.text)
    author_query = surname if len(surname) >= 3 else ""
    candidates: list[SecondaryLookupCandidate] = []
    min_keep_score = 0.12

    def url_priority(url: str) -> int:
        u = (url or "").lower()
        if "stampdf/getpdf.jsp" in u or u.endswith(".pdf") or "/content/pdf/" in u:
            return 0
        if "/ielx" in u and "arnumber=" in u:
            return 1
        if "doi.org/" in u:
            return 3
        return 2

    try:
        crossref_params: dict[str, str | int] = {"query.bibliographic": expected, "rows": 5}
        if author_query:
            crossref_params["query.author"] = author_query
        if ref_year:
            crossref_params["filter"] = f"from-pub-date:{ref_year}-01-01,until-pub-date:{ref_year}-12-31"
        sem = api_limiter.acquire("api.crossref.org") if api_limiter is not None else None
        try:
            res = session.get(
                "https://api.crossref.org/works",
                params=crossref_params,
                timeout=timeout,
            )
        finally:
            if sem is not None:
                sem.release()
        if res.ok:
            items = res.json().get("message", {}).get("items", [])
            for it in items:
                title = ""
                raw_title = it.get("title") or []
                if isinstance(raw_title, list) and raw_title:
                    title = str(raw_title[0] or "")
                doi = (it.get("DOI") or "").strip()
                urls: list[str] = []
                item_url = (it.get("URL") or "").strip()
                if item_url:
                    urls.append(item_url)
                for link in it.get("link", []) or []:
                    link_url = link.get("URL")
                    if link_url:
                        urls.append(str(link_url))
                urls = sorted(unique_preserve_order(urls), key=url_priority)
                if doi or urls:
                    base = secondary_title_score(title, expected)
                    if not title.strip() or base < min_keep_score:
                        continue
                    bonus = 0.0
                    try:
                        year_parts = (it.get("issued", {}) or {}).get("date-parts", []) or []
                        y = int(year_parts[0][0]) if year_parts and year_parts[0] else None
                        if ref_year and y and abs(y - ref_year) <= 0:
                            bonus += 0.08
                    except Exception:
                        pass
                    try:
                        auth = it.get("author") or []
                        if author_query and isinstance(auth, list) and auth:
                            family = str((auth[0] or {}).get("family") or "").lower()
                            if family and family == author_query:
                                bonus += 0.06
                    except Exception:
                        pass
                    candidates.append(
                        SecondaryLookupCandidate(
                            score=base + bonus,
                            doi=doi,
                            urls=urls,
                        )
                    )
    except Exception:
        pass

    try:
        openalex_params: dict[str, str | int] = {"search": expected, "per-page": 5}
        if ref_year:
            openalex_params["filter"] = f"publication_year:{ref_year}"
        sem = api_limiter.acquire("api.openalex.org") if api_limiter is not None else None
        try:
            res = session.get(
                "https://api.openalex.org/works",
                params=openalex_params,
                timeout=timeout,
            )
        finally:
            if sem is not None:
                sem.release()
        if res.ok:
            results = res.json().get("results", [])
            for row in results:
                title = str((row.get("title") or "")).strip()
                doi_url = str((row.get("doi") or "")).strip()
                doi = ""
                urls: list[str] = []
                if doi_url:
                    urls.append(doi_url)
                    if doi_url.lower().startswith("https://doi.org/"):
                        doi = doi_url.split("doi.org/", 1)[1]
                open_access = row.get("open_access", {}) or {}
                oa_url = str((open_access.get("oa_url") or "")).strip()
                if oa_url:
                    urls.append(oa_url)
                primary_location = row.get("primary_location", {}) or {}
                landing = str((primary_location.get("landing_page_url") or "")).strip()
                if landing:
                    urls.append(landing)
                urls = sorted(unique_preserve_order(urls), key=url_priority)
                if doi or urls:
                    base = secondary_title_score(title, expected)
                    if not title.strip() or base < min_keep_score:
                        continue
                    bonus = 0.0
                    try:
                        y = int(row.get("publication_year") or 0) or None
                        if ref_year and y and abs(y - ref_year) <= 0:
                            bonus += 0.08
                    except Exception:
                        pass
                    try:
                        auths = row.get("authorships") or []
                        if author_query and isinstance(auths, list) and auths:
                            name = (((auths[0] or {}).get("author") or {}).get("display_name") or "")
                            if name and author_query in name.lower().split():
                                bonus += 0.06
                    except Exception:
                        pass
                    candidates.append(
                        SecondaryLookupCandidate(
                            score=base + bonus,
                            doi=doi,
                            urls=urls,
                        )
                    )
    except Exception:
        pass

    candidates.sort(key=lambda c: (c.score, 1 if c.doi else 0, len(c.urls)), reverse=True)
    selected = candidates if top_k <= 0 else candidates[: max(1, top_k)]

    dois: list[str] = []
    urls: list[str] = []
    for c in selected:
        if c.doi:
            dois.append(c.doi)
        urls.extend(c.urls)
    return unique_preserve_order(dois), unique_preserve_order(urls)


def lookup_crossref_by_bibliographic(
    session: requests.Session,
    item: ReferenceItem,
    timeout: int,
) -> tuple[list[str], list[str]]:
    """
    使用 Crossref API 按"书目信息"检索（query.bibliographic），尽量补全 DOI/URL。

    注意：
    - 这是二次检索（secondary lookup），只对初次下载失败项使用更合理；
    - 返回值为 (dois, urls)，已去重排序；
    - 网络错误/非 2xx 会直接返回空列表，不中断整体流程。
    """
    query = guess_title_query(item.text)
    params = {"query.bibliographic": query, "rows": 5}
    found_dois: list[str] = []
    found_urls: list[str] = []
    try:
        res = session.get("https://api.crossref.org/works", params=params, timeout=timeout)
        if not res.ok:
            return found_dois, found_urls
        items = res.json().get("message", {}).get("items", [])
        for it in items:
            doi = (it.get("DOI") or "").strip()
            if doi:
                found_dois.append(doi)
            item_url = (it.get("URL") or "").strip()
            if item_url:
                found_urls.append(item_url)
            for link in it.get("link", []) or []:
                link_url = link.get("URL")
                if link_url:
                    found_urls.append(link_url)
    except requests.RequestException:
        pass
    return sorted(set(found_dois)), sorted(set(found_urls))


def lookup_openalex(
    session: requests.Session,
    item: ReferenceItem,
    timeout: int,
) -> tuple[list[str], list[str]]:
    """
    使用 OpenAlex Works 搜索接口尝试补全 DOI/开放获取链接。

    OpenAlex 返回的 doi 字段常见格式为 "https://doi.org/..."，这里会同时：
    - 保存该 URL；
    - 若是 doi.org 链接，则提取出纯 DOI 字符串，加入 found_dois。
    """
    query = guess_title_query(item.text)
    found_dois: list[str] = []
    found_urls: list[str] = []
    try:
        res = session.get(
            "https://api.openalex.org/works",
            params={"search": query, "per-page": 5},
            timeout=timeout,
        )
        if not res.ok:
            return found_dois, found_urls
        results = res.json().get("results", [])
        for row in results:
            doi_url = (row.get("doi") or "").strip()
            if doi_url:
                found_urls.append(doi_url)
                if doi_url.lower().startswith("https://doi.org/"):
                    found_dois.append(doi_url.split("doi.org/", 1)[1])
            open_access = row.get("open_access", {}) or {}
            oa_url = (open_access.get("oa_url") or "").strip()
            if oa_url:
                found_urls.append(oa_url)
            primary_location = row.get("primary_location", {}) or {}
            landing = (primary_location.get("landing_page_url") or "").strip()
            if landing:
                found_urls.append(landing)
    except requests.RequestException:
        pass
    return sorted(set(found_dois)), sorted(set(found_urls))


def lookup_unpaywall(
    session: requests.Session,
    doi: str,
    email: str = "test@example.com",
    timeout: int = 10,
) -> str | None:
    """
    使用 Unpaywall API 查找开放获取PDF链接。

    Unpaywall 是一个免费的开放获取数据库，包含合法的免费PDF链接。
    无需登录/cookies，覆盖大量期刊。

    Args:
        session: requests.Session
        doi: DOI字符串
        email: 邮箱（Unpaywall要求提供，但可以是假邮箱）
        timeout: 超时秒数

    Returns:
        开放获取PDF的URL，如果没有则返回None
    """
    if not doi:
        return None
    try:
        url = f"https://api.unpaywall.org/v2/{quote(doi, safe='')}?email={email}"
        res = session.get(url, timeout=timeout)
        if not res.ok:
            return None
        data = res.json()

        # 检查是否是开放获取
        if data.get("is_oa"):
            # 获取最佳OA位置
            best_oa = data.get("best_oa_location") or {}
            oa_url = best_oa.get("url_for_pdf") or best_oa.get("url")
            if oa_url:
                return oa_url

        # 检查所有OA位置
        for loc in data.get("oa_locations", []) or []:
            oa_url = loc.get("url_for_pdf") or loc.get("url")
            if oa_url:
                return oa_url

    except requests.RequestException:
        pass
    except Exception:
        pass
    return None


def is_neurips_reference(ref_text: str) -> bool:
    s = (ref_text or "").lower()
    if "neural inf. process. syst" in s:
        return True
    if "neural information processing systems" in s:
        return True
    return bool(re.search(r"\b(neurips|nips)\b", s))


def lookup_neurips_proceedings_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    ref_year: int | None,
    timeout: int,
    max_results: int = 5,
) -> list[str]:
    title = re.sub(r"\s+", " ", (expected_title or "").strip().strip(" \t\r\n,.;:，。；："))
    if not title:
        return []
    try:
        res = session.get(
            "https://proceedings.neurips.cc/papers/search",
            params={"q": title},
            timeout=timeout,
        )
        if not res.ok:
            return []
        html = res.text
        links = re.findall(r'href=["\']([^"\']+Abstract[^"\']*\.html)["\']', html, flags=re.IGNORECASE)
        if not links:
            return []
        abs_links = unique_preserve_order([urljoin("https://proceedings.neurips.cc", u) for u in links])
        abs_links = abs_links[: max(1, min(3, int(max_results)))]
        out: list[tuple[int, float, str]] = []
        for page_url in abs_links:
            try:
                page = session.get(page_url, timeout=timeout)
                if not page.ok:
                    continue
                time.sleep(0.25)
                page_html = page.text
                m_title = re.search(
                    r'<meta[^>]+name=["\']citation_title["\'][^>]+content=["\']([^"\']+)["\']',
                    page_html,
                    flags=re.IGNORECASE,
                )
                page_title = re.sub(r"\s+", " ", (m_title.group(1) if m_title else "").strip())
                if not page_title:
                    continue
                score = secondary_title_score(page_title, title)
                if score < 0.6:
                    continue
                m_pdf = re.search(
                    r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+\.pdf)["\']',
                    page_html,
                    flags=re.IGNORECASE,
                )
                pdf_url = (m_pdf.group(1) if m_pdf else "").strip()
                if not pdf_url:
                    m_pdf2 = re.search(r'href=["\']([^"\']+-Paper\.pdf)["\']', page_html, flags=re.IGNORECASE)
                    if m_pdf2:
                        pdf_url = urljoin(page_url, m_pdf2.group(1).strip())
                if not pdf_url:
                    continue
                year_bonus = 0
                m_year = re.search(r"/paper_files/paper/(\d{4})/", page_url)
                if ref_year and m_year:
                    try:
                        y = int(m_year.group(1))
                        if y == int(ref_year):
                            year_bonus = 1
                    except Exception:
                        pass
                out.append((year_bonus, float(score), pdf_url))
            except requests.RequestException:
                continue
            except Exception:
                continue
        out.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return unique_preserve_order([u for _, _, u in out][: max(1, int(max_results))])
    except requests.RequestException:
        return []
    except Exception:
        return []



def lookup_arxiv_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "http://export.arxiv.org/api/query",
            params={
                "search_query": f'ti:\"{title}\"',
                "start": 0,
                "max_results": int(max(1, min(25, max_results))),
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        root = ET.fromstring(res.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out: list[tuple[float, str]] = []
        for entry in root.findall("a:entry", ns):
            entry_title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            entry_title = re.sub(r"\s+", " ", entry_title)
            score = secondary_title_score(entry_title, title)
            if score < 0.6:
                continue
            entry_id = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
            if not entry_id:
                continue
            arxiv_id = entry_id.rsplit("/", 1)[-1]
            arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
            if not arxiv_id:
                continue
            out.append((score, f"https://arxiv.org/pdf/{arxiv_id}.pdf"))
        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_biorxiv_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 bioRxiv/medRxiv 预印本 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # 使用 bioRxiv API 搜索
        res = session.get(
            "https://api.biorxiv.org/details/biorxiv",
            params={
                "title": title[:200],  # 限制标题长度
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        if not data.get("messages") or data["messages"][0].get("status") != "ok":
            return []

        out: list[tuple[float, str]] = []
        for item in data.get("collection", [])[:5]:
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue
            doi = item.get("doi", "")
            if doi:
                # bioRxiv PDF URL 格式
                out.append((score, f"https://www.biorxiv.org/content/{quote(doi, safe='')}.full.pdf"))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_europepmc_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 Europe PMC 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={
                "query": f'TITLE:"{title}"',
                "format": "json",
                "pageSize": 5,
                "openAccess": "true",  # 只返回开放获取
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("resultList", {}).get("result", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 检查是否有开放获取 PDF
            pmcid = item.get("pmcid", "")
            if pmcid:
                out.append((score, f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"))

            # 也检查 DOI
            doi = item.get("doi", "")
            if doi and item.get("isOpenAccess") == "Y":
                # 尝试获取 fullText URL
                full_text_url = item.get("fullTextUrlList", {}).get("fullTextUrl", [])
                for url_info in full_text_url:
                    if url_info.get("documentStyle") == "pdf":
                        out.append((score, url_info.get("url", "")))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_semanticscholar_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 Semantic Scholar 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": title,
                "limit": 5,
                "fields": "title,openAccessPdf",
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("data", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            oa_pdf = item.get("openAccessPdf", {})
            if oa_pdf and oa_pdf.get("url"):
                out.append((score, oa_pdf["url"]))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_core_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 CORE 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # CORE API 搜索
        res = session.get(
            "https://api.core.ac.uk/v3/search/works",
            params={
                "q": f'title:"{title}"',
                "limit": 5,
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("results", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 获取下载URL
            download_url = item.get("downloadUrl", "")
            if download_url and ".pdf" in download_url.lower():
                out.append((score, download_url))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_google_books_pdf_urls(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 Google Books。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={
                "q": f'intitle:"{title}"',
                "maxResults": 5,
                "filter": "free-ebooks",  # 只返回免费电子书
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("items", []):
            volume_info = item.get("volumeInfo", {})
            item_title = (volume_info.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 检查是否有 PDF 下载链接
            access_info = item.get("accessInfo", {})
            pdf_link = access_info.get("pdf", {}).get("downloadLink", "")
            if pdf_link:
                out.append((score, pdf_link))

            # 也检查 webReaderLink
            web_reader = access_info.get("webReaderLink", "")
            if web_reader and access_info.get("viewability") == "ALL_PAGES":
                out.append((score, web_reader))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_crossref_tdm_urls(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过 Crossref TDM (Text and Data Mining) API 查找 PDF 链接。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://api.crossref.org/works",
            params={
                "query.title": title,
                "rows": 5,
                "select": "title,link,DOI",
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("message", {}).get("items", []):
            titles = item.get("title", [])
            item_title = titles[0] if titles else ""
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 检查 link 字段
            for link in item.get("link", []):
                content_type = link.get("content-type", "")
                url = link.get("URL", "")
                if url and "pdf" in content_type.lower():
                    out.append((score, url))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_ssrn_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 SSRN 预印本 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # SSRN 搜索 API
        res = session.get(
            "https://api.ssrn.com/content/v1/papers/search",
            params={
                "query": f'title:"{title}"',
                "limit": 5,
            },
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("papers", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            ssrn_id = item.get("ssrn_id") or item.get("paperId")
            if ssrn_id:
                # SSRN PDF URL 格式
                out.append((score, f"https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID/{ssrn_id}.pdf"))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_chemrxiv_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 ChemRxiv 预印本 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # ChemRxiv 使用 Figshare API
        res = session.get(
            "https://api.figshare.com/v2/articles/search",
            params={
                "search": f'title:"{title}"',
                "resource_type": "publication",
                "institution": 513,  # ChemRxiv institution ID
                "page_size": 5,
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data:
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 获取 PDF 链接
            for file_info in item.get("files", []):
                if file_info.get("is_link_only") is False:
                    pdf_url = file_info.get("download_url", "")
                    if pdf_url:
                        out.append((score, pdf_url))
                        break

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_researchgate_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 ResearchGate 开放获取 PDF。

    注意：ResearchGate 没有官方 API，此函数尝试解析搜索页面。
    成功率可能较低，但作为补充来源。
    """
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # ResearchGate 搜索页面
        res = session.get(
            "https://www.researchgate.net/search",
            params={
                "q": title[:200],
                "type": "publication",
            },
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
            },
        )
        if not res.ok:
            return []

        html = res.text
        out: list[tuple[float, str]] = []

        # 尝试从页面提取 PDF 链接
        # 匹配 publication 页面中的 PDF 链接
        pdf_patterns = [
            r'href="(/publication/\d+_[^"]+\.pdf)"',
            r'href="(/profile/[^/]+/publication/\d+_[^"]+\.pdf)"',
            r'"pdfUrl"\s*:\s*"([^"]+)"',
        ]

        for pattern in pdf_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches[:3]:
                if match.startswith("/"):
                    match = f"https://www.researchgate.net{match}"
                out.append((0.6, match))

        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_unpaywall_by_title(
    session: requests.Session,
    title: str,
    email: str = "test@example.com",
    timeout: int = 10,
) -> list[str]:
    """通过标题搜索 Unpaywall 开放获取 PDF。

    先通过 Crossref 查找 DOI，再通过 Unpaywall 查找 PDF。
    """
    title = (title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # 先通过 Crossref 查找 DOI
        res = session.get(
            "https://api.crossref.org/works",
            params={
                "query.title": title,
                "rows": 3,
            },
            timeout=timeout,
        )
        if not res.ok:
            return []

        data = res.json()
        out: list[str] = []

        for item in data.get("message", {}).get("items", []):
            doi = item.get("DOI", "")
            if not doi:
                continue

            # 检查标题匹配度
            item_title = (item.get("title", [""])[0] if isinstance(item.get("title"), list) else item.get("title", "") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 通过 Unpaywall 查找 PDF
            oa_url = lookup_unpaywall(session, doi, email=email, timeout=timeout)
            if oa_url:
                out.append(oa_url)

        return unique_preserve_order(out)
    except Exception:
        return []


def lookup_openalex_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 OpenAlex 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # OpenAlex 使用 search 参数进行标题搜索
        res = session.get(
            "https://api.openalex.org/works",
            params={
                "search": title,  # 直接使用标题搜索
                "per-page": 5,
                "mailto": "api@example.com",  # Polite pool
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("results", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 检查开放获取
            oa_status = item.get("open_access", {})
            if oa_status.get("is_oa"):
                oa_url = oa_status.get("oa_url", "")
                if oa_url:
                    out.append((score, oa_url))

            # 也检查 locations
            for location in item.get("locations", []):
                pdf_url = location.get("pdf_url", "")
                if pdf_url:
                    out.append((score, pdf_url))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_semanticscholar_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 Semantic Scholar 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": title,
                "limit": 5,
                "fields": "title,openAccessPdf",
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("data", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            oa_pdf = item.get("openAccessPdf", {})
            if oa_pdf and oa_pdf.get("url"):
                out.append((score, oa_pdf["url"]))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def iter_candidate_urls(item: ReferenceItem, use_doi: bool = True) -> Iterable[str]:
    def candidate_priority(url: str) -> int:
        u = (url or "").lower()
        if "stampdf/getpdf.jsp" in u or u.endswith(".pdf") or "/content/pdf/" in u:
            return 0
        if "/ielx" in u and "arnumber=" in u:
            return 1
        if "doi.org/" in u:
            return 3
        return 2

    """
    生成"候选下载链接"序列。

    顺序策略：
    - 先尝试条目文本中直接提取出的 URL（更可能是直链）；
    - 再尝试 DOI 解析链接（doi.org），通常会跳转到出版方页面或 PDF。
    """
    for url in sorted(item.urls, key=candidate_priority):
        normalized = normalize_candidate_url(url)
        if normalized:
            yield normalized
    if use_doi:
        for doi in item.dois:
            d = str(doi or "").strip()
            if not d:
                continue
            d_lower = d.lower()

            # Springer - 直接 PDF 链接
            if d_lower.startswith("10.1007/"):
                yield f"https://link.springer.com/content/pdf/{quote(d, safe='')}.pdf"

            # IOP Science - 直接 PDF 链接
            elif d_lower.startswith("10.1088/"):
                yield f"https://iopscience.iop.org/article/{quote(d, safe='')}/pdf"

            # AIP Publishing - 直接 PDF 链接
            elif d_lower.startswith("10.1063/"):
                yield f"https://pubs.aip.org/aip/pdf/article/{quote(d, safe='')}/pdf"

            # APS (Physical Review) - 直接 PDF 链接
            elif d_lower.startswith("10.1103/"):
                yield f"https://journals.aps.org/prl/pdf/{quote(d, safe='')}"

            # Royal Society - 直接 PDF 链接
            elif d_lower.startswith("10.1098/"):
                yield f"https://royalsocietypublishing.org/doi/pdf/{quote(d, safe='')}"

            # Cambridge - 直接 PDF 链接
            elif d_lower.startswith("10.1017/"):
                yield f"https://www.cambridge.org/core/services/aop-cambridge-core/content/view/{quote(d, safe='')}"

            # Nature - 直接 PDF 链接
            elif d_lower.startswith("10.1038/"):
                yield f"https://www.nature.com/articles/{quote(d, safe='')}.pdf"

            # Science - 直接 PDF 链接
            elif d_lower.startswith("10.1126/"):
                yield f"https://www.science.org/doi/pdf/{quote(d, safe='')}"

            # Wiley - 直接 PDF 链接
            elif d_lower.startswith("10.1002/"):
                yield f"https://onlinelibrary.wiley.com/doi/pdfdirect/{quote(d, safe='')}"

            # Taylor & Francis - 直接 PDF 链接
            elif d_lower.startswith("10.1080/"):
                yield f"https://www.tandfonline.com/doi/pdf/{quote(d, safe='')}"

            # Elsevier/ScienceDirect - 直接 PDF 链接
            elif d_lower.startswith("10.1016/"):
                yield f"https://www.sciencedirect.com/science/article/pii/{quote(d.split('/')[-1], safe='')}/pdfft"

            # Annual Reviews - 直接 PDF 链接
            elif d_lower.startswith("10.1146/"):
                yield f"https://www.annualreviews.org/doi/pdf/{quote(d, safe='')}"

            # ACS Publications - 直接 PDF 链接
            elif d_lower.startswith("10.1021/"):
                yield f"https://pubs.acs.org/doi/pdf/{quote(d, safe='')}"

            # IEEE - 直接 PDF 链接
            elif d_lower.startswith("10.1109/"):
                # IEEE DOI 格式: 10.1109/xxx.202x.xxxxxxx
                yield f"https://ieeexplore.ieee.org/document/{quote(d.split('/')[-1], safe='')}"

            # ACM Digital Library - 直接 PDF 链接
            elif d_lower.startswith("10.1145/"):
                yield f"https://dl.acm.org/doi/pdf/{quote(d, safe='')}"

            # Oxford University Press - 直接 PDF 链接
            elif d_lower.startswith("10.1093/"):
                yield f"https://academic.oup.com/article-pdf/{quote(d, safe='')}"

            # PNAS - 直接 PDF 链接
            elif d_lower.startswith("10.1073/"):
                yield f"https://www.pnas.org/doi/pdf/{quote(d, safe='')}"

            # PLoS - 直接 PDF 链接
            elif d_lower.startswith("10.1371/"):
                yield f"https://journals.plos.org/plosone/article/file?id={quote(d, safe='')}&type=printable"

            # JSTOR - 直接 PDF 链接
            elif d_lower.startswith("10.2307/"):
                jstor_id = d.split("/")[-1]
                yield f"https://www.jstor.org/stable/pdf/{quote(jstor_id, safe='')}.pdf"

            # Frontiers - 直接 PDF 链接
            elif d_lower.startswith("10.3389/"):
                yield f"https://www.frontiersin.org/articles/{quote(d, safe='')}/pdf"

            # MDPI - 直接 PDF 链接
            elif d_lower.startswith("10.3390/"):
                yield f"https://www.mdpi.com/{quote(d, safe='')}/pdf"

            # Hindawi - 直接 PDF 链接
            elif d_lower.startswith("10.1155/"):
                yield f"https://downloads.hindawi.com/journals/{quote(d, safe='')}.pdf"

            # arXiv DOI (10.48550/arXiv.xxxx)
            elif d_lower.startswith("10.48550/"):
                arxiv_id = d.split("/")[-1]
                yield f"https://arxiv.org/pdf/{quote(arxiv_id, safe='')}.pdf"

            # 最后尝试通用 DOI 解析
            yield f"https://doi.org/{quote(d, safe=':/')}"


def normalize_generic_download_sites(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw_values = [x.strip() for x in raw.split(",")]
    elif isinstance(raw, list):
        raw_values = [str(x).strip() for x in raw]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        if not value:
            continue
        if not value.startswith(("http://", "https://")):
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def build_generic_site_candidates(item: ReferenceItem, generic_download_sites: list[str] | None) -> list[str]:
    sites = normalize_generic_download_sites(generic_download_sites)
    if not sites:
        return []
    title = guess_title_query(item.text)
    out: list[str] = []
    seen: set[str] = set()
    for template in sites:
        # If template contains DOI placeholders, expand one URL per DOI.
        if "{doi" in template:
            for doi in item.dois:
                d = str(doi or "").strip()
                if not d:
                    continue
                built = (
                    template.replace("{doi}", d)
                    .replace("{doi_encoded}", quote(d, safe=""))
                    .replace("{title}", title)
                    .replace("{title_encoded}", quote(title, safe=""))
                )
                normalized = normalize_candidate_url(built)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    out.append(normalized)
            continue
        built = (
            template.replace("{title}", title)
            .replace("{title_encoded}", quote(title, safe=""))
        )
        normalized = normalize_candidate_url(built)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def iter_candidate_urls_with_generic_sites(
    item: ReferenceItem,
    use_doi: bool = True,
    generic_download_sites: list[str] | None = None,
) -> Iterable[str]:
    for url in iter_candidate_urls(item, use_doi=use_doi):
        yield url
    for url in build_generic_site_candidates(item, generic_download_sites):
        yield url


def extract_springer_pdf_url(html_text: str, base_url: str) -> str | None:
    m = re.search(
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if m:
        return urljoin(base_url, m.group(1).strip())
    p = urlparse(base_url)
    if (p.hostname or "").lower() != "link.springer.com":
        return None
    m2 = re.search(r"^/(article|chapter)/([^/?#]+)", p.path, flags=re.IGNORECASE)
    if not m2:
        return None
    doi = m2.group(2)
    return f"https://link.springer.com/content/pdf/{doi}.pdf"


def extract_ieee_arnumber(url: str) -> str | None:
    try:
        p = urlparse(url)
        if (p.hostname or "").lower() != "ieeexplore.ieee.org":
            return None
        m = re.search(r"^/document/(\d+)/?", p.path)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def extract_ieee_pdf_url(html_text: str, base_url: str, arnumber: str) -> str | None:
    iframe_match = re.search(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", html_text, flags=re.IGNORECASE)
    if iframe_match:
        iframe_src = iframe_match.group(1).strip()
        if "stampPDF/getPDF.jsp" in iframe_src and f"arnumber={arnumber}" in iframe_src:
            return urljoin(base_url, iframe_src)

    m0 = re.search(
        r"(https?://ieeexplore\.ieee\.org/stampPDF/getPDF\.jsp[^\"'\s>]*arnumber="
        + re.escape(arnumber)
        + r"[^\"'\s>]*)",
        html_text,
        flags=re.IGNORECASE,
    )
    if m0:
        return m0.group(1)
    m0b = re.search(
        r"(/stampPDF/getPDF\.jsp[^\"'\s>]*arnumber=" + re.escape(arnumber) + r"[^\"'\s>]*)",
        html_text,
        flags=re.IGNORECASE,
    )
    if m0b:
        return urljoin(base_url, m0b.group(1))

    m = re.search(r"https?://ieeexplore\.ieee\.org/ielx[^\"]+?arnumber=" + re.escape(arnumber), html_text, flags=re.IGNORECASE)
    if m:
        return m.group(0)
    m2 = re.search(r"(/ielx[^\"]+?arnumber=" + re.escape(arnumber) + r")", html_text, flags=re.IGNORECASE)
    if m2:
        return urljoin(base_url, m2.group(1))
    return None

def resolve_downloads_subdir(downloads_dir: Path, subdir: str) -> Path | None:
    name = str(subdir or "").strip()
    if not name:
        return None
    d = downloads_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def verify_downloaded_pdf_and_update_item(
    *,
    item: ReferenceItem,
    out_file: Path,
    downloads_dir: Path,
    verified_dir: Path | None,
    mismatch_dir: Path | None,
    final_url: str,
    candidate_url: str,
    status_code: int,
    content_type: str,
    phase: str,
    logger: DownloadLogger,
    verify_title_threshold: float,
    verify_rename_mode: str,
    verify_weights,
) -> bool:
    prefix = f"{item.number:03d}"
    expected = guess_title_query(item.text)
    ref_year = parse_ref_year(item.text)
    surname = parse_first_author_surname(item.text)
    decision = verify_and_rename_pdf(
        prefix=prefix,
        out_file=out_file,
        downloads_dir=downloads_dir,
        verified_dir=verified_dir,
        mismatch_dir=mismatch_dir,
        expected_title=expected,
        ref_year=ref_year,
        surname=surname,
        verify_title_threshold=float(verify_title_threshold),
        verify_rename_mode=str(verify_rename_mode or "number_and_original"),
        verify_weights=verify_weights,
    )
    if decision.outcome == "downloaded_pdf":
        item.download_status = "downloaded_pdf"
        item.downloaded_file = decision.rel_path
        item.note = (
            f"{final_url} | title_match={decision.score:.3f} | title_score={decision.title_score:.3f} | line_score={decision.line_score:.3f} | year_hit={int(decision.year_hit)} | author_hit={int(decision.author_hit)}"
        )
        logger.add(
            DownloadAttempt(
                phase=phase,
                ref_number=item.number,
                candidate_url=candidate_url,
                final_url=final_url,
                status_code=int(status_code),
                content_type=content_type,
                outcome="downloaded_pdf",
                waited_seconds=0.0,
                error="",
            )
        )
        return True
    logger.add(
        DownloadAttempt(
            phase=phase,
            ref_number=item.number,
            candidate_url=candidate_url,
            final_url=final_url,
            status_code=int(status_code),
            content_type=content_type,
            outcome="pdf_title_mismatch",
            waited_seconds=0.0,
            error=f"score={decision.score:.3f}; title_score={decision.title_score:.3f}; line_score={decision.line_score:.3f}; year_hit={int(decision.year_hit)}; author_hit={int(decision.author_hit)}; best_line={decision.best_line[:120]}; pdf_title={decision.pdf_title[:120]}",
        )
    )
    return True


def collect_stream_text(first_chunk: bytes, chunks: Iterable[bytes], limit_bytes: int = 1024 * 1024 * 2) -> str:
    buf = bytearray()
    if first_chunk:
        buf.extend(first_chunk[: min(len(first_chunk), 1024 * 1024)])
    for chunk in chunks:
        if not chunk:
            continue
        remaining = limit_bytes - len(buf)
        if remaining <= 0:
            break
        buf.extend(chunk[:remaining])
        if len(buf) >= limit_bytes:
            break
    return buf.decode("utf-8", errors="ignore")


def handle_springer_html(
    *,
    session: requests.Session,
    item: ReferenceItem,
    downloads_dir: Path,
    mismatch_dir: Path | None,
    verified_dir: Path | None,
    timeout: int,
    attempt: int,
    verify_title_rename: bool,
    verify_title_threshold: float,
    verify_rename_mode: str,
    logger: DownloadLogger,
    phase: str,
    seen: set[str],
    prefix: str,
    final_url: str,
    first_chunk: bytes,
    chunks: Iterable[bytes],
) -> bool:
    html_text = collect_stream_text(first_chunk, chunks)
    pdf_url = extract_springer_pdf_url(html_text, base_url=final_url)
    if not pdf_url or pdf_url in seen:
        return False
    seen.add(pdf_url)

    pdf_response: requests.Response | None = None
    try:
        pdf_response = session.get(
            pdf_url,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        )
        if pdf_response.status_code in (408, 425, 429, 500, 502, 503, 504):
            retry_after = parse_retry_after_seconds(pdf_response.headers.get("retry-after") or "")
            waited_s = retry_after if retry_after is not None else min(30.0, (2.0**attempt) + random.random() * 0.25)
            logger.add(
                DownloadAttempt(
                    phase=phase,
                    ref_number=item.number,
                    candidate_url=pdf_url,
                    final_url=pdf_response.url or "",
                    status_code=int(pdf_response.status_code),
                    content_type=(pdf_response.headers.get("content-type") or ""),
                    outcome="retry_status",
                    waited_seconds=float(waited_s),
                    error="",
                )
            )
            time.sleep(waited_s)
            return False
        if not pdf_response.ok:
            logger.add(
                DownloadAttempt(
                    phase=phase,
                    ref_number=item.number,
                    candidate_url=pdf_url,
                    final_url=pdf_response.url or "",
                    status_code=int(pdf_response.status_code),
                    content_type=(pdf_response.headers.get("content-type") or ""),
                    outcome="http_error",
                    waited_seconds=0.0,
                    error="",
                )
            )
            return False

        final_pdf_url = pdf_response.url or pdf_url
        pdf_chunks = pdf_response.iter_content(chunk_size=1024 * 64)
        pdf_first_chunk = b""
        for chunk in pdf_chunks:
            if chunk:
                pdf_first_chunk = chunk
                break
        if not (pdf_first_chunk and is_probably_pdf(pdf_first_chunk)):
            return False

        out_file = downloads_dir / f"{prefix}.pdf"
        tmp_file = downloads_dir / f"{prefix}.pdf.part"
        try:
            with tmp_file.open("wb") as f:
                f.write(pdf_first_chunk)
                for chunk in pdf_chunks:
                    if chunk:
                        f.write(chunk)
            tmp_file.replace(out_file)
            if verify_title_rename:
                expected = guess_title_query(item.text)
                pdf_title = extract_pdf_title_from_file(out_file) or ""
                title_score = title_match_score(pdf_title, expected)
                line_score, best_line = extract_pdf_best_line_score(out_file, expected)
                page_text = extract_pdf_first_page_text(out_file).lower()
                ref_year = parse_ref_year(item.text)
                surname = parse_first_author_surname(item.text)
                year_hit = bool(ref_year) and str(ref_year) in page_text
                author_hit = bool(surname) and surname in page_text
                score = max(title_score, line_score)
                if ref_year and not year_hit:
                    score = score * 0.95
                if surname and not author_hit:
                    score = score * 0.97
                if score >= float(verify_title_threshold):
                    item.download_status = "downloaded_pdf"
                    item.downloaded_file = out_file.name
                    item.note = final_pdf_url
                    name_source = best_line if line_score > title_score and best_line else pdf_title
                    target_name = build_verified_pdf_name(
                        prefix=prefix,
                        original_name=(name_source or expected),
                        rename_mode=str(verify_rename_mode or "number_and_original"),
                    )
                    renamed = unique_path(downloads_dir / target_name)
                    if renamed.name != out_file.name:
                        out_file.replace(renamed)
                        out_file = renamed
                        item.downloaded_file = out_file.name
                    out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
                    item.downloaded_file = rel_path
                    item.note = f"{final_pdf_url} | title_match={score:.3f} | title_score={title_score:.3f} | line_score={line_score:.3f} | year_hit={int(year_hit)} | author_hit={int(author_hit)}"
                    logger.add(
                        DownloadAttempt(
                            phase=phase,
                            ref_number=item.number,
                            candidate_url=pdf_url,
                            final_url=final_pdf_url,
                            status_code=int(pdf_response.status_code),
                            content_type=(pdf_response.headers.get("content-type") or ""),
                            outcome="downloaded_pdf",
                            waited_seconds=0.0,
                            error="",
                        )
                    )
                    return True
                mismatch_file = unique_path((mismatch_dir or downloads_dir) / f"{prefix}__mismatch.pdf")
                out_file.replace(mismatch_file)
                logger.add(
                    DownloadAttempt(
                        phase=phase,
                        ref_number=item.number,
                        candidate_url=pdf_url,
                        final_url=final_pdf_url,
                        status_code=int(pdf_response.status_code),
                        content_type=(pdf_response.headers.get("content-type") or ""),
                        outcome="pdf_title_mismatch",
                        waited_seconds=0.0,
                        error=f"score={score:.3f}; title_score={title_score:.3f}; line_score={line_score:.3f}; year_hit={int(year_hit)}; author_hit={int(author_hit)}; best_line={best_line[:120]}; pdf_title={pdf_title[:120]}",
                    )
                )
                return True

            item.download_status = "downloaded_pdf"
            item.downloaded_file = out_file.name
            item.note = final_pdf_url
            logger.add(
                DownloadAttempt(
                    phase=phase,
                    ref_number=item.number,
                    candidate_url=pdf_url,
                    final_url=final_pdf_url,
                    status_code=int(pdf_response.status_code),
                    content_type=(pdf_response.headers.get("content-type") or ""),
                    outcome="downloaded_pdf",
                    waited_seconds=0.0,
                    error="",
                )
            )
            return True
        finally:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
    finally:
        if pdf_response is not None:
            pdf_response.close()


def handle_ieee_html(
    *,
    session: requests.Session,
    item: ReferenceItem,
    downloads_dir: Path,
    mismatch_dir: Path | None,
    verified_dir: Path | None,
    timeout: int,
    attempt: int,
    verify_title_rename: bool,
    verify_title_threshold: float,
    verify_rename_mode: str,
    logger: DownloadLogger,
    phase: str,
    seen: set[str],
    prefix: str,
    final_url: str,
    first_chunk: bytes,
    chunks: Iterable[bytes],
) -> bool:
    arnumber = extract_ieee_arnumber(final_url)
    if not arnumber:
        return False
    stamp_urls = [
        f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={arnumber}",
        f"https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber={arnumber}",
    ]
    for stamp_url in stamp_urls:
        if stamp_url in seen:
            continue
        seen.add(stamp_url)
        pdf_response: requests.Response | None = None
        try:
            pdf_response = session.get(
                stamp_url,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
            if pdf_response.status_code in (408, 425, 429, 500, 502, 503, 504):
                retry_after = parse_retry_after_seconds(pdf_response.headers.get("retry-after") or "")
                waited_s = retry_after if retry_after is not None else min(30.0, (2.0**attempt) + random.random() * 0.25)
                logger.add(
                    DownloadAttempt(
                        phase=phase,
                        ref_number=item.number,
                        candidate_url=stamp_url,
                        final_url=pdf_response.url or "",
                        status_code=int(pdf_response.status_code),
                        content_type=(pdf_response.headers.get("content-type") or ""),
                        outcome="retry_status",
                        waited_seconds=float(waited_s),
                        error="",
                    )
                )
                time.sleep(waited_s)
                continue
            if not pdf_response.ok:
                logger.add(
                    DownloadAttempt(
                        phase=phase,
                        ref_number=item.number,
                        candidate_url=stamp_url,
                        final_url=pdf_response.url or "",
                        status_code=int(pdf_response.status_code),
                        content_type=(pdf_response.headers.get("content-type") or ""),
                        outcome="http_error",
                        waited_seconds=0.0,
                        error="",
                    )
                )
                continue

            stamp_final_url = pdf_response.url or stamp_url
            pdf_chunks = pdf_response.iter_content(chunk_size=1024 * 64)
            pdf_first_chunk = b""
            for chunk in pdf_chunks:
                if chunk:
                    pdf_first_chunk = chunk
                    break
            if pdf_first_chunk and is_probably_pdf(pdf_first_chunk):
                out_file = downloads_dir / f"{prefix}.pdf"
                tmp_file = downloads_dir / f"{prefix}.pdf.part"
                try:
                    with tmp_file.open("wb") as f:
                        f.write(pdf_first_chunk)
                        for chunk in pdf_chunks:
                            if chunk:
                                f.write(chunk)
                    tmp_file.replace(out_file)
                    if verify_title_rename:
                        expected = guess_title_query(item.text)
                        pdf_title = extract_pdf_title_from_file(out_file) or ""
                        title_score = title_match_score(pdf_title, expected)
                        line_score, best_line = extract_pdf_best_line_score(out_file, expected)
                        page_text = extract_pdf_first_page_text(out_file).lower()
                        ref_year = parse_ref_year(item.text)
                        surname = parse_first_author_surname(item.text)
                        year_hit = bool(ref_year) and str(ref_year) in page_text
                        author_hit = bool(surname) and surname in page_text
                        score = max(title_score, line_score)
                        if ref_year and not year_hit:
                            score = score * 0.95
                        if surname and not author_hit:
                            score = score * 0.97
                        if score >= float(verify_title_threshold):
                            item.download_status = "downloaded_pdf"
                            item.downloaded_file = out_file.name
                            item.note = stamp_final_url
                            name_source = best_line if line_score > title_score and best_line else pdf_title
                            target_name = build_verified_pdf_name(
                                prefix=prefix,
                                original_name=(name_source or expected),
                                rename_mode=str(verify_rename_mode or "number_and_original"),
                            )
                            renamed = unique_path(downloads_dir / target_name)
                            if renamed.name != out_file.name:
                                out_file.replace(renamed)
                                out_file = renamed
                                item.downloaded_file = out_file.name
                            out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
                            item.downloaded_file = rel_path
                            item.note = f"{stamp_final_url} | title_match={score:.3f} | title_score={title_score:.3f} | line_score={line_score:.3f} | year_hit={int(year_hit)} | author_hit={int(author_hit)}"
                            logger.add(
                                DownloadAttempt(
                                    phase=phase,
                                    ref_number=item.number,
                                    candidate_url=stamp_url,
                                    final_url=stamp_final_url,
                                    status_code=int(pdf_response.status_code),
                                    content_type=(pdf_response.headers.get("content-type") or ""),
                                    outcome="downloaded_pdf",
                                    waited_seconds=0.0,
                                    error="",
                                )
                            )
                            return True
                        mismatch_file = unique_path((mismatch_dir or downloads_dir) / f"{prefix}__mismatch.pdf")
                        out_file.replace(mismatch_file)
                        logger.add(
                            DownloadAttempt(
                                phase=phase,
                                ref_number=item.number,
                                candidate_url=stamp_url,
                                final_url=stamp_final_url,
                                status_code=int(pdf_response.status_code),
                                content_type=(pdf_response.headers.get("content-type") or ""),
                                outcome="pdf_title_mismatch",
                                waited_seconds=0.0,
                                error=f"score={score:.3f}; title_score={title_score:.3f}; line_score={line_score:.3f}; year_hit={int(year_hit)}; author_hit={int(author_hit)}; best_line={best_line[:120]}; pdf_title={pdf_title[:120]}",
                            )
                        )
                        return True

                    item.download_status = "downloaded_pdf"
                    item.downloaded_file = out_file.name
                    item.note = stamp_final_url
                    logger.add(
                        DownloadAttempt(
                            phase=phase,
                            ref_number=item.number,
                            candidate_url=stamp_url,
                            final_url=stamp_final_url,
                            status_code=int(pdf_response.status_code),
                            content_type=(pdf_response.headers.get("content-type") or ""),
                            outcome="downloaded_pdf",
                            waited_seconds=0.0,
                            error="",
                        )
                    )
                    return True
                finally:
                    if tmp_file.exists():
                        tmp_file.unlink(missing_ok=True)

            html_text = collect_stream_text(pdf_first_chunk, pdf_chunks)
            direct_pdf_url = extract_ieee_pdf_url(html_text, base_url=stamp_final_url, arnumber=arnumber)
            if direct_pdf_url and direct_pdf_url not in seen:
                seen.add(direct_pdf_url)
                direct_response: requests.Response | None = None
                try:
                    direct_response = session.get(
                        direct_pdf_url,
                        timeout=timeout,
                        stream=True,
                        allow_redirects=True,
                    )
                    if not direct_response.ok:
                        continue
                    final_pdf_url = direct_response.url or direct_pdf_url
                    direct_chunks = direct_response.iter_content(chunk_size=1024 * 64)
                    direct_first = b""
                    for chunk in direct_chunks:
                        if chunk:
                            direct_first = chunk
                            break
                    if not (direct_first and is_probably_pdf(direct_first)):
                        continue
                    out_file = downloads_dir / f"{prefix}.pdf"
                    tmp_file = downloads_dir / f"{prefix}.pdf.part"
                    try:
                        with tmp_file.open("wb") as f:
                            f.write(direct_first)
                            for chunk in direct_chunks:
                                if chunk:
                                    f.write(chunk)
                        tmp_file.replace(out_file)
                        if verify_title_rename:
                            expected = guess_title_query(item.text)
                            pdf_title = extract_pdf_title_from_file(out_file) or ""
                            title_score = title_match_score(pdf_title, expected)
                            line_score, best_line = extract_pdf_best_line_score(out_file, expected)
                            page_text = extract_pdf_first_page_text(out_file).lower()
                            ref_year = parse_ref_year(item.text)
                            surname = parse_first_author_surname(item.text)
                            year_hit = bool(ref_year) and str(ref_year) in page_text
                            author_hit = bool(surname) and surname in page_text
                            score = max(title_score, line_score)
                            if ref_year and not year_hit:
                                score = score * 0.95
                            if surname and not author_hit:
                                score = score * 0.97
                            if score >= float(verify_title_threshold):
                                item.download_status = "downloaded_pdf"
                                item.downloaded_file = out_file.name
                                item.note = final_pdf_url
                                name_source = best_line if line_score > title_score and best_line else pdf_title
                                target_name = build_verified_pdf_name(
                                    prefix=prefix,
                                    original_name=(name_source or expected),
                                    rename_mode=str(verify_rename_mode or "number_and_original"),
                                )
                                renamed = unique_path(downloads_dir / target_name)
                                if renamed.name != out_file.name:
                                    out_file.replace(renamed)
                                    out_file = renamed
                                    item.downloaded_file = out_file.name
                                out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
                                item.downloaded_file = rel_path
                                item.note = f"{final_pdf_url} | title_match={score:.3f} | title_score={title_score:.3f} | line_score={line_score:.3f} | year_hit={int(year_hit)} | author_hit={int(author_hit)}"
                                logger.add(
                                    DownloadAttempt(
                                        phase=phase,
                                        ref_number=item.number,
                                        candidate_url=direct_pdf_url,
                                        final_url=final_pdf_url,
                                        status_code=int(direct_response.status_code),
                                        content_type=(direct_response.headers.get("content-type") or ""),
                                        outcome="downloaded_pdf",
                                        waited_seconds=0.0,
                                        error="",
                                    )
                                )
                                return True
                            mismatch_file = unique_path((mismatch_dir or downloads_dir) / f"{prefix}__mismatch.pdf")
                            out_file.replace(mismatch_file)
                            logger.add(
                                DownloadAttempt(
                                    phase=phase,
                                    ref_number=item.number,
                                    candidate_url=direct_pdf_url,
                                    final_url=final_pdf_url,
                                    status_code=int(direct_response.status_code),
                                    content_type=(direct_response.headers.get("content-type") or ""),
                                    outcome="pdf_title_mismatch",
                                    waited_seconds=0.0,
                                    error=f"score={score:.3f}; title_score={title_score:.3f}; line_score={line_score:.3f}; year_hit={int(year_hit)}; author_hit={int(author_hit)}; best_line={best_line[:120]}; pdf_title={pdf_title[:120]}",
                                )
                            )
                            return True

                        item.download_status = "downloaded_pdf"
                        item.downloaded_file = out_file.name
                        item.note = final_pdf_url
                        logger.add(
                            DownloadAttempt(
                                phase=phase,
                                ref_number=item.number,
                                candidate_url=direct_pdf_url,
                                final_url=final_pdf_url,
                                status_code=int(direct_response.status_code),
                                content_type=(direct_response.headers.get("content-type") or ""),
                                outcome="downloaded_pdf",
                                waited_seconds=0.0,
                                error="",
                            )
                        )
                        return True
                    finally:
                        if tmp_file.exists():
                            tmp_file.unlink(missing_ok=True)
                finally:
                    if direct_response is not None:
                        direct_response.close()
        finally:
            if pdf_response is not None:
                pdf_response.close()
    return False


def try_download(
    session: requests.Session,
    item: ReferenceItem,
    downloads_dir: Path,
    meta_dir: Path | None,
    landing_dir: Path | None,
    mismatch_dir: Path | None,
    timeout: int,
    retries: int,
    use_doi: bool,
    max_candidates_per_item: int,
    domain_limiter: DomainLimiter,
    logger: DownloadLogger,
    phase: str,
    verify_title_rename: bool,
    verify_title_threshold: float,
    verify_rename_mode: str,
    verify_weights: VerifyWeights | dict | None,
    verified_dir: Path | None,
    generic_download_sites: list[str] | None = None,
) -> None:
    """
    尝试为单条参考文献下载 PDF，或保存落地页 URL。

    输出策略：
    - 总是写入 00X_meta.txt（条目原文，便于人工追溯）；
    - 若请求返回 PDF（Content-Type 或 url 后缀判断），保存为 00X.pdf；
    - 否则保存最终跳转的落地页 URL 到 00X_landing.url.txt。
    """
    prefix = f"{item.number:03d}"
    if item.download_status in {"downloaded_pdf", "saved_landing_url"} and item.downloaded_file:
        existing = downloads_dir / item.downloaded_file
        if existing.exists():
            return
    meta_file = (meta_dir or downloads_dir) / f"{prefix}_meta.txt"
    meta_file.write_text(item.text + "\n", encoding="utf-8")

    seen: set[str] = set()
    tried = 0
    best_landing_url = ""
    best_landing_candidate = ""
    best_landing_status_code = 0
    best_landing_content_type = ""
    for candidate in iter_candidate_urls_with_generic_sites(
        item,
        use_doi=use_doi,
        generic_download_sites=generic_download_sites,
    ):
        if candidate in seen:
            continue
        seen.add(candidate)
        tried += 1
        if max_candidates_per_item > 0 and tried > max_candidates_per_item:
            break

        for attempt in range(max(1, retries)):
            try:
                host = urlparse(candidate).hostname or ""
                sem = domain_limiter.acquire(host)
                response: requests.Response | None = None
                waited_s = 0.0
                try:
                    response = session.get(
                        candidate,
                        timeout=timeout,
                        stream=True,
                        allow_redirects=True,
                    )

                    if response.status_code in (408, 425, 429, 500, 502, 503, 504):
                        retry_after = parse_retry_after_seconds(response.headers.get("retry-after") or "")
                        waited_s = retry_after if retry_after is not None else min(30.0, (2.0**attempt) + random.random() * 0.25)
                        logger.add(
                            DownloadAttempt(
                                phase=phase,
                                ref_number=item.number,
                                candidate_url=candidate,
                                final_url=response.url or "",
                                status_code=int(response.status_code),
                                content_type=(response.headers.get("content-type") or ""),
                                outcome="retry_status",
                                waited_seconds=float(waited_s),
                                error="",
                            )
                        )
                        domain_limiter.backoff(host, waited_s)
                        time.sleep(waited_s)
                        continue

                    if not response.ok:
                        content_type = (response.headers.get("content-type") or "")
                        if should_record_landing_url(int(response.status_code), content_type):
                            final_url = response.url or candidate
                            if final_url:
                                best_landing_url = final_url
                                best_landing_candidate = candidate
                                best_landing_status_code = int(response.status_code)
                                best_landing_content_type = content_type
                        logger.add(
                            DownloadAttempt(
                                phase=phase,
                                ref_number=item.number,
                                candidate_url=candidate,
                                final_url=response.url or "",
                                status_code=int(response.status_code),
                                content_type=content_type,
                                outcome="http_error",
                                waited_seconds=0.0,
                                error="",
                            )
                        )
                        continue
                    final_url = response.url or candidate
                    if final_url:
                        best_landing_url = final_url
                        best_landing_candidate = candidate
                        best_landing_status_code = int(response.status_code)
                    chunks = response.iter_content(chunk_size=1024 * 64)
                    first_chunk = b""
                    for chunk in chunks:
                        if chunk:
                            first_chunk = chunk
                            break

                    is_pdf_confirmed = bool(first_chunk) and is_probably_pdf(first_chunk)
                    if is_pdf_confirmed:
                        out_file = downloads_dir / f"{prefix}.pdf"
                        tmp_file = downloads_dir / f"{prefix}.pdf.part"
                        try:
                            with tmp_file.open("wb") as f:
                                f.write(first_chunk)
                                for chunk in chunks:
                                    if chunk:
                                        f.write(chunk)
                            tmp_file.replace(out_file)
                            if verify_title_rename:
                                verify_downloaded_pdf_and_update_item(
                                    item=item,
                                    out_file=out_file,
                                    downloads_dir=downloads_dir,
                                    verified_dir=verified_dir,
                                    mismatch_dir=mismatch_dir,
                                    final_url=final_url,
                                    candidate_url=candidate,
                                    status_code=int(response.status_code),
                                    content_type=(response.headers.get("content-type") or ""),
                                    phase=phase,
                                    logger=logger,
                                    verify_title_threshold=float(verify_title_threshold),
                                    verify_rename_mode=str(verify_rename_mode),
                                    verify_weights=verify_weights,
                                )
                                if item.download_status == "downloaded_pdf":
                                    return
                                break

                            item.download_status = "downloaded_pdf"
                            item.downloaded_file = out_file.name
                            item.note = final_url
                            logger.add(
                                DownloadAttempt(
                                    phase=phase,
                                    ref_number=item.number,
                                    candidate_url=candidate,
                                    final_url=final_url,
                                    status_code=int(response.status_code),
                                    content_type=(response.headers.get("content-type") or ""),
                                    outcome="downloaded_pdf",
                                    waited_seconds=0.0,
                                    error="",
                                )
                            )
                            return
                        finally:
                            if tmp_file.exists():
                                tmp_file.unlink(missing_ok=True)

                    content_type = (response.headers.get("content-type") or "")
                    best_landing_content_type = content_type
                    if "text/html" in content_type.lower():
                        host = (urlparse(final_url).hostname or "").lower()
                        helpers = {
                            "parse_retry_after_seconds": parse_retry_after_seconds,
                            "is_probably_pdf": is_probably_pdf,
                            "verify_downloaded_pdf_and_update_item": verify_downloaded_pdf_and_update_item,
                            "extract_springer_pdf_url": extract_springer_pdf_url,
                            "extract_ieee_arnumber": extract_ieee_arnumber,
                            "extract_ieee_pdf_url": extract_ieee_pdf_url,
                            "DownloadAttempt": DownloadAttempt,
                        }
                        handler_result = site_handlers.dispatch_html(
                            host=host,
                            session=session,
                            item=item,
                            helpers=helpers,
                            downloads_dir=downloads_dir,
                            mismatch_dir=mismatch_dir,
                            verified_dir=verified_dir,
                            timeout=timeout,
                            attempt=attempt,
                            verify_title_rename=verify_title_rename,
                            verify_title_threshold=float(verify_title_threshold),
                            verify_rename_mode=str(verify_rename_mode),
                            verify_weights=verify_weights,
                            logger=logger,
                            phase=phase,
                            seen=seen,
                            prefix=prefix,
                            final_url=final_url,
                            first_chunk=first_chunk,
                            chunks=chunks,
                        )
                        if handler_result == "downloaded":
                            return
                        if handler_result == "retry":
                            continue
                        break

                    break
                finally:
                    if response is not None:
                        response.close()
                    domain_limiter.release(sem)
            except requests.RequestException as e:
                waited_s = min(30.0, (2.0**attempt) + random.random() * 0.25)
                logger.add(
                    DownloadAttempt(
                        phase=phase,
                        ref_number=item.number,
                        candidate_url=candidate,
                        final_url="",
                        status_code=0,
                        content_type="",
                        outcome="request_exception",
                        waited_seconds=float(waited_s),
                        error=str(e),
                    )
                )
                domain_limiter.backoff(urlparse(candidate).hostname or "", waited_s)
                time.sleep(waited_s)
                continue

    if best_landing_url:
        landing_base = landing_dir or downloads_dir
        landing_file = landing_base / f"{prefix}_landing.url.txt"
        landing_file.write_text(best_landing_url + "\n", encoding="utf-8")
        item.download_status = "saved_landing_url"
        item.downloaded_file = landing_file.relative_to(downloads_dir).as_posix() if landing_base != downloads_dir else landing_file.name
        item.note = best_landing_url
        logger.add(
            DownloadAttempt(
                phase=phase,
                ref_number=item.number,
                candidate_url=best_landing_candidate or "",
                final_url=best_landing_url,
                status_code=int(best_landing_status_code),
                content_type=best_landing_content_type,
                outcome="saved_landing_url",
                waited_seconds=0.0,
                error="",
            )
        )
        return

    item.download_status = "failed"
    item.note = "No reachable URL/DOI PDF or landing page."


def run_initial_download_phase(
    refs: list[ReferenceItem],
    downloads_dir: Path,
    meta_dir: Path | None,
    landing_dir: Path | None,
    mismatch_dir: Path | None,
    timeout: int,
    retries: int,
    use_doi: bool,
    max_candidates_per_item: int,
    workers: int,
    show_progress: bool,
    user_agent: str,
    max_per_domain: int,
    min_domain_delay_ms: int,
    logger: DownloadLogger,
    cookies_jar: MozillaCookieJar | None,
    verify_title_rename: bool,
    verify_title_threshold: float,
    verify_rename_mode: str,
    verify_weights: VerifyWeights | dict | None,
    verified_dir: Path | None,
    domain_cookies: dict[str, MozillaCookieJar] | None = None,
    generic_download_sites: list[str] | None = None,
) -> None:
    """
    初次下载阶段：并发尝试每条参考文献的候选链接。

    关键点：
    - 使用 thread_local 为每个线程保存一个 Session，实现连接复用；
    - show_progress 为 True 且安装 tqdm 时，会显示进度条。
    - domain_cookies: 按域名配置的cookies，优先于全局cookies使用
    """
    if not refs:
        return

    thread_local = threading.local()
    domain_limiter = DomainLimiter(max_per_domain, min_delay_ms=min_domain_delay_ms)
    domain_cookies = domain_cookies or {}

    def get_session_for_item(item: ReferenceItem) -> requests.Session:
        """根据item的URL/DOI域名选择合适的session"""
        # 检查item的URLs和DOIs，找到匹配的域名cookies
        from urllib.parse import urlparse
        for url in item.urls:
            host = urlparse(url).hostname or ""
            host_lower = host.lower()
            # 检查是否有直接匹配的域名cookies
            if host_lower in domain_cookies:
                if not hasattr(thread_local, "domain_sessions"):
                    thread_local.domain_sessions = {}
                if host_lower not in thread_local.domain_sessions:
                    thread_local.domain_sessions[host_lower] = make_session(
                        pool_size=max(8, workers * 2),
                        user_agent=user_agent,
                        cookies_jar=domain_cookies[host_lower],
                    )
                return thread_local.domain_sessions[host_lower]
        # 使用默认session
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session(pool_size=max(8, workers * 2), user_agent=user_agent, cookies_jar=cookies_jar)
        return thread_local.session

    def worker(item: ReferenceItem) -> None:
        # 根据item的域名选择合适的session
        session = get_session_for_item(item)
        try_download(
            session=session,
            item=item,
            downloads_dir=downloads_dir,
            meta_dir=meta_dir,
            landing_dir=landing_dir,
            mismatch_dir=mismatch_dir,
            timeout=timeout,
            retries=retries,
            use_doi=use_doi,
            max_candidates_per_item=max_candidates_per_item,
            domain_limiter=domain_limiter,
            logger=logger,
            phase="initial",
            verify_title_rename=verify_title_rename,
            verify_title_threshold=verify_title_threshold,
            verify_rename_mode=verify_rename_mode,
            verify_weights=verify_weights,
            verified_dir=verified_dir,
            generic_download_sites=generic_download_sites,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(worker, item) for item in refs]
        iterator = as_completed(futures)
        if show_progress and tqdm is not None:
            iterator = tqdm(iterator, total=len(futures), desc="Initial download")
        for _ in iterator:
            pass


def enrich_failed_references(
    refs: list[ReferenceItem],
    timeout: int,
    lookup_timeout: int,
    retries: int,
    downloads_dir: Path,
    meta_dir: Path | None,
    landing_dir: Path | None,
    mismatch_dir: Path | None,
    max_items: int,
    max_candidates_per_item: int,
    secondary_top_k: int,
    workers: int,
    show_progress: bool,
    user_agent: str,
    max_per_domain: int,
    min_domain_delay_ms: int,
    logger: DownloadLogger,
    cookies_jar: MozillaCookieJar | None,
    verify_title_rename: bool,
    verify_title_threshold: float,
    verify_rename_mode: str,
    verify_weights: VerifyWeights | dict | None,
    verified_dir: Path | None,
    secondary_cache: SecondaryLookupCache | None,
    unpaywall_email: str = "",
    generic_download_sites: list[str] | None = None,
    api_concurrency: int = 1,
    api_min_delay_ms: int = 500,
    neurips_proceedings: bool = True,
) -> None:
    """
    二次检索阶段：对初次下载失败的条目调用 Crossref/OpenAlex 补全 DOI/URL，再重试下载。

    说明：
    - max_items 用于限制二次检索的数量，避免对外部 API 造成过大压力；
    - 一旦二次下载成功，会在 note 中追加 resolved_by=secondary_lookup 标记。
    """
    failed = [r for r in refs if r.download_status == "failed"]
    if max_items > 0:
        failed = failed[:max_items]
    if not failed:
        return

    thread_local = threading.local()
    domain_limiter = DomainLimiter(max_per_domain, min_delay_ms=min_domain_delay_ms)
    api_limiter = DomainLimiter(int(max(1, api_concurrency)), min_delay_ms=int(max(0, api_min_delay_ms)))

    def worker(item: ReferenceItem) -> None:
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session(pool_size=max(8, workers * 2), user_agent=user_agent, cookies_jar=cookies_jar)
        session = thread_local.session
        cache_key = hashlib.sha1(
            json.dumps(
                {
                    "v": 2,
                    "q": guess_title_query(item.text),
                    "y": parse_ref_year(item.text),
                    "a": parse_first_author_surname(item.text),
                    "k": int(secondary_top_k),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cached = secondary_cache.get(cache_key) if secondary_cache is not None else None
        if cached is not None:
            secondary_dois, secondary_urls = cached
        else:
            secondary_dois, secondary_urls = lookup_secondary_ranked(
                session,
                item=item,
                timeout=lookup_timeout,
                top_k=secondary_top_k,
                api_limiter=api_limiter,
            )
            if secondary_cache is not None:
                secondary_cache.set(cache_key, secondary_dois, secondary_urls)
        item.dois = unique_preserve_order(list(item.dois) + list(secondary_dois))
        item.urls = unique_preserve_order(list(item.urls) + list(secondary_urls))

        if neurips_proceedings and is_neurips_reference(item.text):
            expected = guess_title_query(item.text)
            ref_year = parse_ref_year(item.text)
            neurips_urls = lookup_neurips_proceedings_pdf_urls_by_title(
                session,
                expected,
                ref_year,
                timeout=lookup_timeout,
            )
            if neurips_urls:
                item.urls = unique_preserve_order(list(neurips_urls) + list(item.urls))
                for u in neurips_urls[:3]:
                    logger.add(
                        DownloadAttempt(
                            phase="secondary",
                            ref_number=item.number,
                            candidate_url="neurips:search",
                            final_url=u,
                            status_code=0,
                            content_type="application/pdf",
                            outcome="neurips_injected",
                            waited_seconds=0.0,
                            error="",
                        )
                    )
                if secondary_cache is not None:
                    secondary_cache.set(cache_key, item.dois, item.urls)
            else:
                arxiv_urls = lookup_arxiv_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
                if arxiv_urls:
                    item.urls = unique_preserve_order(list(arxiv_urls) + list(item.urls))
                    for u in arxiv_urls[:3]:
                        logger.add(
                            DownloadAttempt(
                                phase="secondary",
                                ref_number=item.number,
                                candidate_url="arxiv:title",
                                final_url=u,
                                status_code=0,
                                content_type="application/pdf",
                                outcome="arxiv_injected",
                                waited_seconds=0.0,
                                error="",
                            )
                        )
                    if secondary_cache is not None:
                        secondary_cache.set(cache_key, item.dois, item.urls)

        # 尝试更多预印本服务器和开放获取来源
        expected = guess_title_query(item.text)

        # Semantic Scholar 开放获取 PDF
        s2_urls = lookup_semanticscholar_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
        if s2_urls:
            item.urls = unique_preserve_order(list(s2_urls) + list(item.urls))
            for u in s2_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="semanticscholar:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="s2_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # Europe PMC 开放获取
        pmc_urls = lookup_europepmc_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
        if pmc_urls:
            item.urls = unique_preserve_order(list(pmc_urls) + list(item.urls))
            for u in pmc_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="europepmc:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="pmc_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # bioRxiv/medRxiv 预印本
        biorxiv_urls = lookup_biorxiv_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
        if biorxiv_urls:
            item.urls = unique_preserve_order(list(biorxiv_urls) + list(item.urls))
            for u in biorxiv_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="biorxiv:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="biorxiv_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # CORE 开放获取
        core_urls = lookup_core_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
        if core_urls:
            item.urls = unique_preserve_order(list(core_urls) + list(item.urls))
            for u in core_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="core:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="core_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # OpenAlex 开放获取
        openalex_urls = lookup_openalex_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
        if openalex_urls:
            item.urls = unique_preserve_order(list(openalex_urls) + list(item.urls))
            for u in openalex_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="openalex:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="openalex_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # Crossref TDM API
        crossref_tdm_urls = lookup_crossref_tdm_urls(session, expected, timeout=lookup_timeout)
        if crossref_tdm_urls:
            item.urls = unique_preserve_order(list(crossref_tdm_urls) + list(item.urls))
            for u in crossref_tdm_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="crossref_tdm:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="crossref_tdm_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # Google Books（用于书籍）
        gbooks_urls = lookup_google_books_pdf_urls(session, expected, timeout=lookup_timeout)
        if gbooks_urls:
            item.urls = unique_preserve_order(list(gbooks_urls) + list(item.urls))
            for u in gbooks_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="googlebooks:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="gbooks_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # SSRN 预印本
        ssrn_urls = lookup_ssrn_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
        if ssrn_urls:
            item.urls = unique_preserve_order(list(ssrn_urls) + list(item.urls))
            for u in ssrn_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="ssrn:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="ssrn_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # ChemRxiv 预印本
        chemrxiv_urls = lookup_chemrxiv_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
        if chemrxiv_urls:
            item.urls = unique_preserve_order(list(chemrxiv_urls) + list(item.urls))
            for u in chemrxiv_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="chemrxiv:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="chemrxiv_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # ResearchGate（补充来源）
        rg_urls = lookup_researchgate_pdf_urls_by_title(session, expected, timeout=lookup_timeout)
        if rg_urls:
            item.urls = unique_preserve_order(list(rg_urls) + list(item.urls))
            for u in rg_urls[:2]:
                logger.add(
                    DownloadAttempt(
                        phase="secondary",
                        ref_number=item.number,
                        candidate_url="researchgate:title",
                        final_url=u,
                        status_code=0,
                        content_type="application/pdf",
                        outcome="researchgate_injected",
                        waited_seconds=0.0,
                        error="",
                    )
                )

        # 尝试Unpaywall获取开放获取PDF，并记录可观测事件
        if unpaywall_email:
            for doi in item.dois:
                oa_url = lookup_unpaywall(session, doi, email=unpaywall_email, timeout=lookup_timeout)
                api_url = f"https://api.unpaywall.org/v2/{quote(doi, safe='')}"
                if oa_url:
                    logger.add(
                        DownloadAttempt(
                            phase="secondary",
                            ref_number=item.number,
                            candidate_url=api_url,
                            final_url=oa_url,
                            status_code=0,
                            content_type="",
                            outcome="unpaywall_candidate",
                            waited_seconds=0.0,
                            error="",
                        )
                    )
                    if oa_url not in item.urls:
                        item.urls = unique_preserve_order(list(item.urls) + [oa_url])
                        logger.add(
                            DownloadAttempt(
                                phase="secondary",
                                ref_number=item.number,
                                candidate_url=oa_url,
                                final_url="",
                                status_code=0,
                                content_type="",
                                outcome="unpaywall_injected",
                                waited_seconds=0.0,
                                error="",
                            )
                        )
                else:
                    logger.add(
                        DownloadAttempt(
                            phase="secondary",
                            ref_number=item.number,
                            candidate_url=api_url,
                            final_url="",
                            status_code=0,
                            content_type="",
                            outcome="unpaywall_miss",
                            waited_seconds=0.0,
                            error="",
                        )
                    )

            # 如果没有 DOI，尝试通过标题搜索 Unpaywall
            if not item.dois and expected:
                unpaywall_title_urls = lookup_unpaywall_by_title(session, expected, email=unpaywall_email, timeout=lookup_timeout)
                if unpaywall_title_urls:
                    item.urls = unique_preserve_order(list(unpaywall_title_urls) + list(item.urls))
                    for u in unpaywall_title_urls[:2]:
                        logger.add(
                            DownloadAttempt(
                                phase="secondary",
                                ref_number=item.number,
                                candidate_url="unpaywall:title",
                                final_url=u,
                                status_code=0,
                                content_type="application/pdf",
                                outcome="unpaywall_title_injected",
                                waited_seconds=0.0,
                                error="",
                            )
                        )

        if item.dois or item.urls:
            try_download(
                session=session,
                item=item,
                downloads_dir=downloads_dir,
                meta_dir=meta_dir,
                landing_dir=landing_dir,
                mismatch_dir=mismatch_dir,
                timeout=timeout,
                retries=retries,
                use_doi=True,
                max_candidates_per_item=max_candidates_per_item,
                domain_limiter=domain_limiter,
                logger=logger,
                phase="secondary",
                verify_title_rename=verify_title_rename,
                verify_title_threshold=verify_title_threshold,
                verify_rename_mode=verify_rename_mode,
                verify_weights=verify_weights,
                verified_dir=verified_dir,
                generic_download_sites=generic_download_sites,
            )
            if item.download_status != "failed":
                item.note = f"{item.note} | resolved_by=secondary_lookup".strip()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(worker, item) for item in failed]
        iterator = as_completed(futures)
        if show_progress and tqdm is not None:
            iterator = tqdm(iterator, total=len(futures), desc="Secondary lookup")
        for _ in iterator:
            pass
    if secondary_cache is not None:
        secondary_cache.flush()


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


def load_domain_cookies_config(path: Path) -> dict[str, dict]:
    """
    加载域名cookies配置文件

    Returns:
        {"domain": {"cookies_path": "...", "description": "..."}}
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 支持 {"domains": {...}} 和直接 {"domain": {...}} 两种格式
        if isinstance(data, dict):
            if "domains" in data:
                return data.get("domains", {})
            return {k: v for k, v in data.items() if isinstance(v, dict) and "cookies_path" in v}
    except Exception:
        pass
    return {}


def save_domain_cookies_config(config: dict[str, dict], path: Path) -> None:
    """保存域名cookies配置到文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "domains": config,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def suggest_cookies_configuration(
    refs: list[ReferenceItem],
    domain_cookies_config: dict[str, dict],
    output_dir: Path,
) -> None:
    """
    分析失败的参考文献，提示用户配置机构 cookies。

    根据失败条目的域名，生成配置建议，帮助用户了解哪些机构 cookies 可能有助于提高下载成功率。
    """
    # 收集失败条目的域名
    failed_domains: dict[str, int] = {}  # domain -> count
    failed_refs_by_domain: dict[str, list[int]] = {}  # domain -> [ref_numbers]
    no_source_count = 0  # 无 DOI/URL 的失败条目数

    for ref in refs:
        if ref.download_status != "failed":
            continue

        has_source = False

        # 从 DOI 提取域名
        for doi in ref.dois:
            domain = guess_publisher_domain_from_doi(doi)
            if domain:
                failed_domains[domain] = failed_domains.get(domain, 0) + 1
                if domain not in failed_refs_by_domain:
                    failed_refs_by_domain[domain] = []
                failed_refs_by_domain[domain].append(ref.number)
                has_source = True

        # 从 URL 提取域名
        for url in ref.urls:
            try:
                parsed = urlparse(url)
                hostname = (parsed.hostname or "").lower()
                if hostname:
                    # 移除 www. 前缀
                    if hostname.startswith("www."):
                        hostname = hostname[4:]
                    # 跳过通用域名
                    if hostname in ("doi.org", "arxiv.org", "semanticscholar.org"):
                        continue
                    failed_domains[hostname] = failed_domains.get(hostname, 0) + 1
                    if hostname not in failed_refs_by_domain:
                        failed_refs_by_domain[hostname] = []
                    failed_refs_by_domain[hostname].append(ref.number)
                    has_source = True
            except Exception:
                pass

        # 如果没有 DOI/URL，尝试从文本中提取出版商
        if not has_source:
            no_source_count += 1
            publisher_domain = guess_publisher_from_ref_text(ref.text)
            if publisher_domain:
                failed_domains[publisher_domain] = failed_domains.get(publisher_domain, 0) + 1
                if publisher_domain not in failed_refs_by_domain:
                    failed_refs_by_domain[publisher_domain] = []
                failed_refs_by_domain[publisher_domain].append(ref.number)

    if not failed_domains and no_source_count == 0:
        return

    # 合并相似域名（如 www.xxx.com 和 xxx.com）
    merged_domains: dict[str, int] = {}
    for domain, count in failed_domains.items():
        # 标准化域名
        normalized = domain
        if normalized.startswith("www."):
            normalized = normalized[4:]

        # 检查是否已有相同域名的不同形式
        found_key = None
        for existing_domain in merged_domains:
            existing_normalized = existing_domain
            if existing_normalized.startswith("www."):
                existing_normalized = existing_normalized[4:]
            if existing_normalized == normalized:
                found_key = existing_domain
                break

        if found_key:
            merged_domains[found_key] += count
        else:
            merged_domains[domain] = count

    failed_domains = merged_domains

    # 过滤掉已有有效 cookies 配置的域名
    domains_need_cookies: dict[str, tuple[int, str]] = {}  # domain -> (count, description)

    for domain, count in sorted(failed_domains.items(), key=lambda x: x[1], reverse=True):
        # 检查是否已有配置
        config = domain_cookies_config.get(domain, {})
        cookies_path = config.get("cookies_path", "")

        # 检查 cookies 文件是否存在
        cookies_exists = False
        if cookies_path:
            path = Path(cookies_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            cookies_exists = path.exists()

        if not cookies_exists:
            description = config.get("description", guess_publisher_name_from_domain(domain))
            domains_need_cookies[domain] = (count, description)

    # 生成提示信息
    print("\n" + "=" * 60)
    print("机构 Cookies 配置建议")
    print("=" * 60)

    total_failed = sum(1 for r in refs if r.download_status == "failed")
    print(f"\n共有 {total_failed} 篇参考文献下载失败。")

    if no_source_count > 0:
        print(f"其中 {no_source_count} 篇无法识别来源（无 DOI/URL）。")

    if domains_need_cookies:
        print("以下机构可能需要配置 cookies 才能访问：\n")

        # 按失败数量排序显示
        for i, (domain, (count, description)) in enumerate(
            sorted(domains_need_cookies.items(), key=lambda x: x[1][0], reverse=True)[:15], 1
        ):
            status = "未配置" if domain not in domain_cookies_config else "cookies文件不存在"
            print(f"  {i:2d}. {domain:35s} {count:3d} 篇  [{status}]")
            if description and description != domain:
                print(f"      └─ {description}")

    # 生成配置建议
    print("\n" + "-" * 60)
    print("配置方法：")
    print("-" * 60)
    print("""
1. 在浏览器中登录机构网站（如学校图书馆）
2. 使用浏览器扩展导出 cookies（推荐 "EditThisCookie" 或 "Cookie Editor"）
3. 将 cookies 保存为 Netscape 格式文件（如 cookies/springer.txt）
4. 编辑 domain_cookies.json 文件，添加配置：

示例配置：
{
  "version": 1,
  "domains": {
    "link.springer.com": {
      "cookies_path": "cookies/springer.json",
      "description": "Springer Link"
    },
    "www.sciencedirect.com": {
      "cookies_path": "cookies/elsevier.json",
      "description": "ScienceDirect (Elsevier)"
    }
  }
}
""")

    # 自动生成配置模板
    if domains_need_cookies:
        suggested_config_path = output_dir / "suggested_cookies_config.json"
        suggested_config: dict[str, dict] = {}

        for domain, (count, description) in domains_need_cookies.items():
            # 为常见域名生成建议的 cookies 路径
            cookie_filename = domain.replace(".", "_").replace("www_", "")
            suggested_config[domain] = {
                "cookies_path": f"cookies/{cookie_filename}.json",
                "description": description,
                "failed_count": count,
            }

        # 保存建议配置
        suggested_config_path.parent.mkdir(parents=True, exist_ok=True)
        suggested_data = {
            "version": 1,
            "_comment": "此文件为建议的 cookies 配置模板，请根据实际情况修改后使用",
            "domains": suggested_config,
        }
        suggested_config_path.write_text(json.dumps(suggested_data, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n已生成建议配置文件：{suggested_config_path}")
        print("请参考该文件配置 cookies 后重新运行工具。\n")
    else:
        print("\n提示：失败的条目可能是因为：")
        print("  - 参考文献信息不完整，无法找到对应的论文")
        print("  - 论文不在开放获取范围内")
        print("  - 网络连接问题\n")


def guess_publisher_from_ref_text(ref_text: str) -> str | None:
    """从参考文献文本中猜测出版商域名"""
    text = (ref_text or "").lower()

    # 常见出版商关键词映射
    publisher_keywords: dict[str, str] = {
        # Springer
        "springer": "link.springer.com",
        "springer us": "link.springer.com",
        "springer nature": "link.springer.com",
        # Elsevier
        "elsevier": "www.sciencedirect.com",
        "sciencedirect": "www.sciencedirect.com",
        "north-holland": "www.sciencedirect.com",
        # Wiley
        "wiley": "onlinelibrary.wiley.com",
        "wiley-blackwell": "onlinelibrary.wiley.com",
        "john wiley": "onlinelibrary.wiley.com",
        # Taylor & Francis
        "taylor & francis": "www.tandfonline.com",
        "taylor and francis": "www.tandfonline.com",
        "routledge": "www.tandfonline.com",
        # Oxford
        "oxford university press": "academic.oup.com",
        "oxford": "academic.oup.com",
        # Cambridge
        "cambridge university press": "www.cambridge.org",
        "cambridge": "www.cambridge.org",
        # Nature
        "nature publishing": "www.nature.com",
        "springer nature": "www.nature.com",
        # Science
        "american association for the advancement of science": "www.science.org",
        "aaas": "www.science.org",
        # APS
        "american physical society": "journals.aps.org",
        "physical review": "journals.aps.org",
        # AIP
        "american institute of physics": "pubs.aip.org",
        "aip publishing": "pubs.aip.org",
        # IEEE
        "ieee": "ieeexplore.ieee.org",
        "institute of electrical and electronics engineers": "ieeexplore.ieee.org",
        # ACM
        "acm": "dl.acm.org",
        "association for computing machinery": "dl.acm.org",
        # ACS
        "american chemical society": "pubs.acs.org",
        # Royal Society
        "royal society": "royalsocietypublishing.org",
        # IOP
        "iop publishing": "iopscience.iop.org",
        "institute of physics": "iopscience.iop.org",
        # Annual Reviews
        "annual reviews": "www.annualreviews.org",
        # ASME
        "asme": "asmedigitalcollection.asme.org",
        "american society of mechanical engineers": "asmedigitalcollection.asme.org",
        # AIAA
        "aiaa": "arc.aiaa.org",
        "american institute of aeronautics and astronautics": "arc.aiaa.org",
    }

    for keyword, domain in publisher_keywords.items():
        if keyword in text:
            return domain

    return None


def guess_publisher_domain_from_doi(doi: str) -> str | None:
    """根据 DOI 前缀猜测出版商域名"""
    doi_prefixes: dict[str, str] = {
        "10.1007": "link.springer.com",      # Springer
        "10.1002": "onlinelibrary.wiley.com",  # Wiley
        "10.1016": "www.sciencedirect.com",    # Elsevier
        "10.1021": "pubs.acs.org",             # ACS
        "10.1038": "www.nature.com",           # Nature
        "10.1046": "onlinelibrary.wiley.com",  # Wiley (old)
        "10.1057": "link.springer.com",        # Palgrave Macmillan
        "10.1063": "pubs.aip.org",             # AIP
        "10.1073": "www.pnas.org",             # PNAS
        "10.1080": "www.tandfonline.com",      # Taylor & Francis
        "10.1088": "iopscience.iop.org",       # IOP
        "10.1093": "academic.oup.com",         # Oxford
        "10.1098": "royalsocietypublishing.org",  # Royal Society
        "10.1103": "journals.aps.org",         # APS
        "10.1109": "ieeexplore.ieee.org",      # IEEE
        "10.1111": "onlinelibrary.wiley.com",  # Wiley
        "10.1126": "www.science.org",          # Science
        "10.1145": "dl.acm.org",               # ACM
        "10.1146": "www.annualreviews.org",    # Annual Reviews
        "10.1155": "downloads.hindawi.com",    # Hindawi
        "10.1161": "www.ahajournals.org",      # AHA
        "10.1177": "journals.sagepub.com",     # SAGE
        "10.1186": "www.biomedcentral.com",    # BMC
        "10.1371": "journals.plos.org",        # PLoS
        "10.2307": "www.jstor.org",            # JSTOR
        "10.3389": "www.frontiersin.org",      # Frontiers
        "10.3390": "www.mdpi.com",             # MDPI
        "10.1017": "www.cambridge.org",        # Cambridge
        "10.2139": "www.ssrn.com",             # SSRN
        "10.48550": "arxiv.org",               # arXiv DOI
    }

    doi_lower = (doi or "").lower().strip()
    for prefix, domain in doi_prefixes.items():
        if doi_lower.startswith(prefix):
            return domain
    return None


def guess_publisher_name_from_domain(domain: str) -> str:
    """根据域名猜测出版商名称"""
    domain_names: dict[str, str] = {
        "link.springer.com": "Springer Link",
        "onlinelibrary.wiley.com": "Wiley Online Library",
        "www.sciencedirect.com": "ScienceDirect (Elsevier)",
        "linkinghub.elsevier.com": "Elsevier",
        "pubs.acs.org": "ACS Publications",
        "www.nature.com": "Nature",
        "pubs.aip.org": "AIP Publishing",
        "scitation.aip.org": "AIP Scitation",
        "iopscience.iop.org": "IOP Science",
        "journals.aps.org": "APS Journals",
        "link.aps.org": "APS Journals",
        "royalsocietypublishing.org": "Royal Society Publishing",
        "www.annualreviews.org": "Annual Reviews",
        "asmedigitalcollection.asme.org": "ASME Digital Collection",
        "www.science.org": "Science (AAAS)",
        "www.tandfonline.com": "Taylor & Francis",
        "dl.acm.org": "ACM Digital Library",
        "ieeexplore.ieee.org": "IEEE Xplore",
        "www.cambridge.org": "Cambridge Core",
        "cambridge.org": "Cambridge Core",
        "www.jstor.org": "JSTOR",
        "www.pnas.org": "PNAS",
        "academic.oup.com": "Oxford Academic",
        "journals.plos.org": "PLoS",
        "www.frontiersin.org": "Frontiers",
        "www.mdpi.com": "MDPI",
        "downloads.hindawi.com": "Hindawi",
        "www.ssrn.com": "SSRN",
        "arxiv.org": "arXiv",
        "www.biomedcentral.com": "BioMed Central",
        "journals.sagepub.com": "SAGE Journals",
        "www.ahajournals.org": "AHA Journals",
        "arc.aiaa.org": "AIAA ARC",
    }

    # 直接匹配
    if domain in domain_names:
        return domain_names[domain]

    # 尝试移除 www. 前缀后匹配
    if domain.startswith("www."):
        clean_domain = domain[4:]
        if clean_domain in domain_names:
            return domain_names[clean_domain]

    # 返回原始域名
    return domain


def load_domain_cookies(
    domain_config: dict[str, dict],
    base_dir: Path | None = None,
) -> dict[str, "MozillaCookieJar"]:
    """
    加载每个域名的cookies

    Args:
        domain_config: {"domain": {"cookies_path": "..."}}
        base_dir: cookies路径的基准目录

    Returns:
        {"domain": MozillaCookieJar}
    """
    result: dict[str, MozillaCookieJar] = {}
    for domain, cfg in domain_config.items():
        cookies_path = cfg.get("cookies_path")
        if not cookies_path:
            continue
        path = Path(cookies_path)
        if not path.is_absolute() and base_dir:
            path = base_dir / path
        if path.exists():
            try:
                jar = load_cookies_txt(path)
                result[domain] = jar
            except Exception:
                pass
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（CLI）。"""
    parser = argparse.ArgumentParser(
        description="Extract references from PDF, number them, and download where possible."
    )
    parser.add_argument("--config", help="JSON config file path")
    parser.add_argument("--input", "-i", required=False, help="Input paper PDF path")
    parser.add_argument("--output", "-o", default="references_output", help="Output directory")
    parser.add_argument(
        "--pdf-parser",
        choices=["pypdf", "pdfplumber"],
        default="pdfplumber",
        help="PDF text parser backend (default: pdfplumber; falls back to pypdf if missing)",
    )
    parser.add_argument("--header-margin", type=float, default=40.0, help="Top margin for pdfplumber crop")
    parser.add_argument("--footer-margin", type=float, default=40.0, help="Bottom margin for pdfplumber crop")

    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds")
    parser.add_argument("--lookup-timeout", type=int, default=6, help="Secondary lookup API timeout seconds")
    parser.add_argument("--retries", type=int, default=1, help="Retries per candidate URL")
    parser.add_argument("--cookies", help="cookies.txt (Netscape) path for authenticated downloads")
    parser.add_argument("--verify-title-rename", action="store_true", help="Verify downloaded PDF title and rename on match")
    parser.add_argument(
        "--verify-rename-mode",
        choices=["original", "number_only", "number_and_original"],
        default="number_and_original",
        help="Rename mode when verify-title-rename is enabled",
    )
    parser.add_argument("--verify-title-threshold", type=float, default=0.55, help="Title match threshold (Jaccard)")
    parser.add_argument("--verify-title-weight", type=float, default=1.0, help="Verify score: weight for PDF title match")
    parser.add_argument("--verify-line-weight", type=float, default=1.0, help="Verify score: weight for first-page best line match")
    parser.add_argument("--verify-year-hit-bonus", type=float, default=0.0, help="Verify score: add bonus when year appears on first page")
    parser.add_argument("--verify-year-miss-mult", type=float, default=0.95, help="Verify score: multiply when year missing on first page")
    parser.add_argument("--verify-author-hit-bonus", type=float, default=0.0, help="Verify score: add bonus when author surname appears on first page")
    parser.add_argument("--verify-author-miss-mult", type=float, default=0.97, help="Verify score: multiply when author surname missing on first page")
    parser.add_argument("--verified-subdir", default="verified_pdfs", help="Put verified PDFs into downloads/<subdir> (empty disables)")
    parser.add_argument("--meta-subdir", default="meta", help="Put meta txt files into downloads/<subdir> (empty disables)")
    parser.add_argument("--landing-subdir", default="landing_urls", help="Put landing url txt files into downloads/<subdir> (empty disables)")
    parser.add_argument("--mismatch-subdir", default="mismatch_pdfs", help="Put mismatched PDFs into downloads/<subdir> (empty disables)")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent worker count")
    parser.add_argument("--max-per-domain", type=int, default=2, help="Max concurrent requests per domain (0 means unlimited)")
    parser.add_argument("--min-domain-delay-ms", type=int, default=0, help="Minimum delay per domain between requests")
    parser.add_argument("--user-agent", default="ReferenceDownloader/1.1", help="HTTP User-Agent header")
    parser.add_argument("--download-log", default="download_log.csv", help="Write download attempt log CSV (empty to disable)")
    parser.add_argument("--unpaywall-email", default="", help="Your email for Unpaywall API (required for OA lookup)")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bars")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", dest="resume", action="store_true", help="Resume using existing output directory state")
    resume_group.add_argument("--no-resume", dest="resume", action="store_false", help="Disable resume behavior")
    parser.set_defaults(resume=True)

    parser.add_argument(
        "--max-candidates-per-item",
        type=int,
        default=3,
        help="Max URLs tried per item (0 means unlimited).",
    )
    parser.add_argument("--skip-doi", action="store_true", help="Skip DOI URL attempts in initial phase")
    parser.add_argument(
        "--download-max",
        "--initial-max",
        dest="download_max",
        type=int,
        default=0,
        help="Max references to attempt downloading (0 means all).",
    )

    parser.add_argument(
        "--secondary-lookup",
        action="store_true",
        help="For failed items, query Crossref/OpenAlex and retry.",
    )
    parser.add_argument("--secondary-max", type=int, default=40, help="Secondary phase max failed items")
    parser.add_argument("--secondary-top-k", type=int, default=2, help="Secondary lookup: keep top K title-similar candidates (0 means all)")
    parser.add_argument("--secondary-cache", default="cache/secondary_lookup_cache.json", help="Secondary lookup cache file (relative to output; empty disables)")
    parser.add_argument(
        "--generic-download-sites",
        nargs="*",
        default=[],
        help=(
            "Extra generic download site URL templates, e.g. "
            "'https://sci-hub.se/{doi}' or 'https://example.org/search?q={title_encoded}'"
        ),
    )
    parser.add_argument("--no-download", action="store_true", help="Only extract and number references")

    # 域名cookies配置
    parser.add_argument("--domain-cookies-file", default="domain_cookies.json", help="Per-domain cookies config file")
    parser.add_argument(
        "--interactive",
        choices=["auto", "true", "false"],
        default="auto",
        help="Interactive mode: auto (detect TTY), true, or false (batch mode)",
    )
    # API限速与NeurIPS proceedings注入
    parser.add_argument("--api-concurrency", type=int, default=1, help="API concurrency for secondary lookups")
    parser.add_argument("--api-min-delay-ms", type=int, default=500, help="Min delay between API calls (ms)")
    parser.add_argument(
        "--neurips-proceedings",
        choices=["true", "false"],
        default="true",
        help="Enable NeurIPS proceedings PDF injection in secondary lookup",
    )
    return parser


def main() -> None:
    """
    命令行入口。

    - no_download: 仅解析与导出，不进行任何网络下载；
    - skip_doi: 初次下载阶段不尝试 DOI（减少跳转开销或避免被出版方拦截）；
    - secondary_lookup: 对失败项启用二次检索并重试下载。
    """
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config")
    config_args, _ = config_parser.parse_known_args(sys.argv[1:])

    parser = build_arg_parser()
    if config_args.config:
        config_path = Path(config_args.config)
        config = load_config_file(config_path)
        valid_dests = {a.dest for a in parser._actions}
        filtered = {k: v for k, v in config.items() if k in valid_dests}
        parser.set_defaults(**filtered)
    args = parser.parse_args()
    if not args.input:
        parser.error("--input is required (or set 'input' in --config JSON)")
    cookies_jar: MozillaCookieJar | None = None
    if args.cookies:
        cookies_path = Path(args.cookies)
        if cookies_path.exists():
            cookies_jar = load_cookies_txt(cookies_path)
    input_pdf = Path(args.input)
    output_dir = Path(args.output)
    downloads_dir = output_dir / "downloads"
    meta_dir = resolve_downloads_subdir(downloads_dir, str(getattr(args, "meta_subdir", "meta")))
    landing_dir = resolve_downloads_subdir(downloads_dir, str(getattr(args, "landing_subdir", "landing_urls")))
    mismatch_dir = resolve_downloads_subdir(downloads_dir, str(getattr(args, "mismatch_subdir", "mismatch_pdfs")))
    verified_dir: Path | None = None
    if bool(args.verify_title_rename):
        verified_dir = resolve_downloads_subdir(downloads_dir, str(getattr(args, "verified_subdir", "verified_pdfs")))
    verify_weights = VerifyWeights(
        title_weight=float(getattr(args, "verify_title_weight", 1.0)),
        line_weight=float(getattr(args, "verify_line_weight", 1.0)),
        year_hit_bonus=float(getattr(args, "verify_year_hit_bonus", 0.0)),
        year_miss_multiplier=float(getattr(args, "verify_year_miss_mult", 0.95)),
        author_hit_bonus=float(getattr(args, "verify_author_hit_bonus", 0.0)),
        author_miss_multiplier=float(getattr(args, "verify_author_miss_mult", 0.97)),
    )

    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF does not exist: {input_pdf}")
    if not input_pdf.is_file():
        raise ValueError(f"Input PDF is not a file: {input_pdf}")
    if input_pdf.stat().st_size <= 0:
        raise ValueError(f"Input PDF is empty: {input_pdf}")

    # 1) 读取全文文本（不同后端对页眉页脚/分栏的抗噪能力不同）
    try:
        full_text = read_pdf_text(
            input_pdf,
            parser=args.pdf_parser,
            header_margin=args.header_margin,
            footer_margin=args.footer_margin,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to read input PDF: {input_pdf} ({exc})") from exc
    # 2) 截取参考文献章节，并按风格分段成条目
    ref_section = extract_references_section(full_text)
    refs = split_references(ref_section)

    # 3) 域名分析与交互式配置
    from site_handlers.domain_analyzer import analyze_reference_domains
    from interactive_ui import should_run_interactive, display_domain_summary, configure_cookies_interactively

    # 加载域名cookies配置
    domain_cookies_file = Path(getattr(args, "domain_cookies_file", "domain_cookies.json"))
    if not domain_cookies_file.is_absolute():
        domain_cookies_file = output_dir / domain_cookies_file
    domain_cookies_config = load_domain_cookies_config(domain_cookies_file)

    # 分析域名
    domain_info = analyze_reference_domains(refs, domain_cookies_config)

    # 显示域名摘要（始终显示）
    display_domain_summary(domain_info)

    # 检测是否应该进入交互模式
    interactive_setting = str(getattr(args, "interactive", "auto"))
    is_interactive = should_run_interactive(interactive_setting)

    if is_interactive:
        # 交互式配置cookies
        new_config = configure_cookies_interactively(domain_info, domain_cookies_config)
        if new_config != domain_cookies_config:
            domain_cookies_config = new_config
            # 保存配置
            save_domain_cookies_config(domain_cookies_config, domain_cookies_file)
            print(f"\n已保存域名cookies配置到: {domain_cookies_file}")

    # 加载域名cookies
    domain_cookies = load_domain_cookies(domain_cookies_config, Path.cwd())
    generic_download_sites = normalize_generic_download_sites(getattr(args, "generic_download_sites", []))

    output_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    if bool(getattr(args, "resume", True)):
        apply_resume_state(refs, output_dir=output_dir, downloads_dir=downloads_dir)

    if not args.no_download:
        logger = DownloadLogger()
        secondary_cache: SecondaryLookupCache | None = None
        cache_str = str(getattr(args, "secondary_cache", "") or "").strip()
        if cache_str:
            cache_path = Path(cache_str)
            if not cache_path.is_absolute():
                cache_path = output_dir / cache_path
            secondary_cache = SecondaryLookupCache(cache_path)
        initial_refs = refs[: args.download_max] if args.download_max > 0 else refs
        run_initial_download_phase(
            initial_refs,
            downloads_dir=downloads_dir,
            meta_dir=meta_dir,
            landing_dir=landing_dir,
            mismatch_dir=mismatch_dir,
            timeout=args.timeout,
            retries=args.retries,
            use_doi=not args.skip_doi,
            max_candidates_per_item=args.max_candidates_per_item,
            workers=args.workers,
            show_progress=not args.no_progress,
            user_agent=args.user_agent,
            max_per_domain=args.max_per_domain,
            min_domain_delay_ms=args.min_domain_delay_ms,
            logger=logger,
            cookies_jar=cookies_jar,
            verify_title_rename=bool(args.verify_title_rename),
            verify_title_threshold=float(args.verify_title_threshold),
            verify_rename_mode=str(getattr(args, "verify_rename_mode", "number_and_original")),
            verify_weights=verify_weights,
            verified_dir=verified_dir,
            domain_cookies=domain_cookies,
            generic_download_sites=generic_download_sites,
        )
        if args.secondary_lookup:
            # 4) 二次检索：只针对失败项补全 DOI/URL 再下载
            limited_refs = initial_refs
            enrich_failed_references(
                limited_refs,
                timeout=args.timeout,
                lookup_timeout=args.lookup_timeout,
                retries=args.retries,
                downloads_dir=downloads_dir,
                meta_dir=meta_dir,
                landing_dir=landing_dir,
                mismatch_dir=mismatch_dir,
                max_items=args.secondary_max,
                max_candidates_per_item=args.max_candidates_per_item,
                secondary_top_k=int(args.secondary_top_k),
                workers=args.workers,
                show_progress=not args.no_progress,
                user_agent=args.user_agent,
                max_per_domain=args.max_per_domain,
                min_domain_delay_ms=args.min_domain_delay_ms,
                logger=logger,
                cookies_jar=cookies_jar,
                verify_title_rename=bool(args.verify_title_rename),
                verify_title_threshold=float(args.verify_title_threshold),
                verify_rename_mode=str(getattr(args, "verify_rename_mode", "number_and_original")),
                verify_weights=verify_weights,
                verified_dir=verified_dir,
                secondary_cache=secondary_cache,
                unpaywall_email=str(getattr(args, "unpaywall_email", "") or ""),
                generic_download_sites=generic_download_sites,
                api_concurrency=int(getattr(args, "api_concurrency", 1)),
                api_min_delay_ms=int(getattr(args, "api_min_delay_ms", 500)),
                neurips_proceedings=str(getattr(args, "neurips_proceedings", "true")).lower() == "true",
            )
        if args.download_log:
            logger.write_csv(output_dir / args.download_log)

    # 5) 写出最终结果（refs 里含每条的下载状态与文件名）
    write_outputs(refs, output_dir)

    # 6) 简单汇总输出，便于快速判断成功率
    total = len(refs)
    ok_pdf = sum(1 for r in refs if r.download_status == "downloaded_pdf")
    ok_landing = sum(1 for r in refs if r.download_status == "saved_landing_url")
    failed = sum(1 for r in refs if r.download_status == "failed")
    print(f"Done. Parsed {total} references.")
    print(f"PDF downloaded: {ok_pdf}, landing URLs saved: {ok_landing}, failed: {failed}")
    print(f"Output directory: {output_dir.resolve()}")

    # 7) 提示用户配置机构 cookies
    if failed > 0:
        suggest_cookies_configuration(refs, domain_cookies_config, output_dir)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
