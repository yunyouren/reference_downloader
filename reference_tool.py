#!/usr/bin/env python3
"""
Extract, number, and download references from a paper PDF.

中文说明（概览）
----------------
这个脚本用于从论文 PDF 中：
1) 提取“参考文献/References”章节文本；
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
import random
import re
import sys
import threading
import time
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
# 非数字编号分段时，用于判断“这一行很可能是一个新条目的开头”（APA 风格常见）。
AUTHOR_YEAR_START_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\- ]{0,40},\s+.+\((?:19|20)\d{2}[a-z]?\)"
)
# 非数字编号分段时，用于判断“这一行很可能是一个新条目的开头”（MLA 风格启发式）。
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


class DomainLimiter:
    def __init__(self, max_per_domain: int, min_delay_ms: int) -> None:
        self._max_per_domain = max_per_domain
        self._min_delay_s = max(0.0, float(min_delay_ms) / 1000.0)
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._next_allowed: dict[str, float] = {}

    def __enter__(self) -> "DomainLimiter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

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

        wait_s = 0.0
        if self._min_delay_s > 0:
            now = time.monotonic()
            with self._lock:
                next_allowed = self._next_allowed.get(key, 0.0)
                wait_s = max(0.0, next_allowed - now)
                self._next_allowed[key] = max(next_allowed, now) + self._min_delay_s
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
    if raw.isdigit():
        return float(int(raw))
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

    raw = path.read_text(encoding="utf-8")
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
        return read_pdf_text_pdfplumber(pdf_path, header_margin=header_margin, footer_margin=footer_margin)
    return read_pdf_text_pypdf(pdf_path)


def cleanup_reference_text(text: str) -> str:
    """
    清洗参考文献条目文本。

    目的：
    - 将常见的“智能引号”转为普通引号，便于后续正则处理与输出一致性；
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
    从全文文本中截取“参考文献/References”章节。

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
    解析“数字编号”风格的参考文献。

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
    判断一行文本是否像“非数字编号”参考文献条目的起始行。

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
    解析“非数字编号”风格的参考文献（例如 APA/MLA）。

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
    从参考文献条目中猜测一个“更像标题”的查询字符串，用于二次检索。

    做法：
    - 去掉引号等符号；
    - 按常见分隔符切成片段（.;。；），取最长片段作为候选；
    - 尝试剔除 vol/no/pp 等尾部信息，避免检索噪声；
    - 最终限制长度，避免请求参数过长。
    """
    m = re.findall(r"[\"“”]([^\"“”]{8,300})[\"“”]", ref_text)
    if m:
        best = max((s.strip() for s in m), key=len, default="")
        if best:
            return best[:180].strip()

    tmp = re.sub(r"['\"“”‘’]", "", ref_text)
    parts = [p.strip() for p in re.split(r"[.;。；]", tmp) if p.strip()]
    if not parts:
        return ref_text[:120]
    best = max(parts, key=len)
    best = re.sub(r"\b(?:vol|no|pp|ed|dept|univ|university)\b.*$", "", best, flags=re.IGNORECASE)
    return best[:180].strip()


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
) -> tuple[list[str], list[str]]:
    expected = guess_title_query(item.text)
    candidates: list[SecondaryLookupCandidate] = []

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
        res = session.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": expected, "rows": 5},
            timeout=timeout,
        )
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
                    candidates.append(
                        SecondaryLookupCandidate(
                            score=secondary_title_score(title, expected),
                            doi=doi,
                            urls=urls,
                        )
                    )
    except requests.RequestException:
        pass

    try:
        res = session.get(
            "https://api.openalex.org/works",
            params={"search": expected, "per-page": 5},
            timeout=timeout,
        )
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
                    candidates.append(
                        SecondaryLookupCandidate(
                            score=secondary_title_score(title, expected),
                            doi=doi,
                            urls=urls,
                        )
                    )
    except requests.RequestException:
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
    使用 Crossref API 按“书目信息”检索（query.bibliographic），尽量补全 DOI/URL。

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


def iter_candidate_urls(item: ReferenceItem, use_doi: bool = True) -> Iterable[str]:
    def normalize_candidate(url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return raw
        try:
            p = urlparse(raw)
            host = (p.hostname or "").lower()
            if host == "xplorestaging.ieee.org":
                scheme = "https"
                netloc = "ieeexplore.ieee.org"
                return p._replace(scheme=scheme, netloc=netloc).geturl()
        except Exception:
            return raw
        return raw

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
    生成“候选下载链接”序列。

    顺序策略：
    - 先尝试条目文本中直接提取出的 URL（更可能是直链）；
    - 再尝试 DOI 解析链接（doi.org），通常会跳转到出版方页面或 PDF。
    """
    for url in sorted(item.urls, key=candidate_priority):
        normalized = normalize_candidate(url)
        if normalized:
            yield normalized
    if use_doi:
        for doi in item.dois:
            yield f"https://doi.org/{quote(doi, safe=':/')}"


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


def extract_pdf_title_from_file(pdf_path: Path) -> str | None:
    try:
        reader = PdfReader(str(pdf_path))
        meta = getattr(reader, "metadata", None)
        title = None
        if meta is not None:
            title = getattr(meta, "title", None)
            if not title and isinstance(meta, dict):
                title = meta.get("/Title") or meta.get("Title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        if getattr(reader, "pages", None):
            text = (reader.pages[0].extract_text() or "").strip()
            if text:
                for line in text.splitlines():
                    cleaned = line.strip()
                    if 12 <= len(cleaned) <= 240:
                        return cleaned
    except Exception:
        return None
    return None


def normalize_title_tokens(text: str) -> list[str]:
    raw = (text or "").lower()
    raw = re.sub(r"[\u2010-\u2015\u2212]", "-", raw)
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    tokens = [t for t in raw.split() if len(t) >= 3]
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
    return [t for t in tokens if t not in stop]


def title_match_score(pdf_title: str, expected_title: str) -> float:
    a = set(normalize_title_tokens(pdf_title))
    b = set(normalize_title_tokens(expected_title))
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union <= 0:
        return 0.0
    return float(inter) / float(union)


def sanitize_filename_component(text: str, max_len: int = 90) -> str:
    s = (text or "").strip()
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1F]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    s = s.rstrip(". ")
    return s


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for i in range(2, 200):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem}_{int(time.time())}{suffix}"


def move_verified_pdf(out_file: Path, downloads_dir: Path, verified_dir: Path | None) -> tuple[Path, str]:
    if verified_dir is None:
        return out_file, out_file.name
    verified_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_path(verified_dir / out_file.name)
    if dest.resolve() != out_file.resolve():
        out_file.replace(dest)
    rel = dest.relative_to(downloads_dir).as_posix()
    return dest, rel


def try_download(
    session: requests.Session,
    item: ReferenceItem,
    downloads_dir: Path,
    timeout: int,
    retries: int,
    use_doi: bool,
    max_candidates_per_item: int,
    domain_limiter: DomainLimiter,
    logger: DownloadLogger,
    phase: str,
    verify_title_rename: bool,
    verify_title_threshold: float,
    verified_dir: Path | None,
) -> None:
    """
    尝试为单条参考文献下载 PDF，或保存落地页 URL。

    输出策略：
    - 总是写入 00X_meta.txt（条目原文，便于人工追溯）；
    - 若请求返回 PDF（Content-Type 或 url 后缀判断），保存为 00X.pdf；
    - 否则保存最终跳转的落地页 URL 到 00X_landing.url.txt。
    """
    prefix = f"{item.number:03d}"
    meta_file = downloads_dir / f"{prefix}_meta.txt"
    meta_file.write_text(item.text + "\n", encoding="utf-8")

    seen: set[str] = set()
    tried = 0
    for candidate in iter_candidate_urls(item, use_doi=use_doi):
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
                        time.sleep(waited_s)
                        continue

                    if not response.ok:
                        logger.add(
                            DownloadAttempt(
                                phase=phase,
                                ref_number=item.number,
                                candidate_url=candidate,
                                final_url=response.url or "",
                                status_code=int(response.status_code),
                                content_type=(response.headers.get("content-type") or ""),
                                outcome="http_error",
                                waited_seconds=0.0,
                                error="",
                            )
                        )
                        continue
                    final_url = response.url or candidate
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
                                expected = guess_title_query(item.text)
                                pdf_title = extract_pdf_title_from_file(out_file) or ""
                                score = title_match_score(pdf_title, expected)
                                if score >= float(verify_title_threshold):
                                    item.download_status = "downloaded_pdf"
                                    item.downloaded_file = out_file.name
                                    item.note = final_url
                                    name = sanitize_filename_component(pdf_title or expected)
                                    if name:
                                        renamed = unique_path(downloads_dir / f"{prefix} {name}.pdf")
                                        if renamed.name != out_file.name:
                                            out_file.replace(renamed)
                                            out_file = renamed
                                            item.downloaded_file = out_file.name
                                    out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
                                    item.downloaded_file = rel_path
                                    item.note = f"{final_url} | title_match={score:.3f}"
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
                                else:
                                    mismatch_file = unique_path(downloads_dir / f"{prefix}__mismatch.pdf")
                                    out_file.replace(mismatch_file)
                                    logger.add(
                                        DownloadAttempt(
                                            phase=phase,
                                            ref_number=item.number,
                                            candidate_url=candidate,
                                            final_url=final_url,
                                            status_code=int(response.status_code),
                                            content_type=(response.headers.get("content-type") or ""),
                                            outcome="pdf_title_mismatch",
                                            waited_seconds=0.0,
                                            error=f"score={score:.3f}; pdf_title={pdf_title[:140]}",
                                        )
                                    )
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
                    if "text/html" in content_type.lower() and (urlparse(final_url).hostname or "").lower() == "link.springer.com":
                        collected = bytearray()
                        if first_chunk:
                            collected.extend(first_chunk[:1024 * 1024])
                        limit = 1024 * 1024 * 2
                        for chunk in chunks:
                            if not chunk:
                                continue
                            remaining = limit - len(collected)
                            if remaining <= 0:
                                break
                            collected.extend(chunk[:remaining])
                            if len(collected) >= limit:
                                break
                        html_text = collected.decode("utf-8", errors="ignore")
                        pdf_url = extract_springer_pdf_url(html_text, base_url=final_url)
                        if pdf_url and pdf_url not in seen:
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
                                elif not pdf_response.ok:
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
                                else:
                                    final_pdf_url = pdf_response.url or pdf_url
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
                                                score = title_match_score(pdf_title, expected)
                                                if score >= float(verify_title_threshold):
                                                    item.download_status = "downloaded_pdf"
                                                    item.downloaded_file = out_file.name
                                                    item.note = final_pdf_url
                                                    name = sanitize_filename_component(pdf_title or expected)
                                                    if name:
                                                        renamed = unique_path(downloads_dir / f"{prefix} {name}.pdf")
                                                        if renamed.name != out_file.name:
                                                            out_file.replace(renamed)
                                                            out_file = renamed
                                                            item.downloaded_file = out_file.name
                                                    out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
                                                    item.downloaded_file = rel_path
                                                    item.note = f"{final_pdf_url} | title_match={score:.3f}"
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
                                                    return
                                                else:
                                                    mismatch_file = unique_path(downloads_dir / f"{prefix}__mismatch.pdf")
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
                                                            error=f"score={score:.3f}; pdf_title={pdf_title[:140]}",
                                                        )
                                                    )
                                                    break

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
                                            return
                                        finally:
                                            if tmp_file.exists():
                                                tmp_file.unlink(missing_ok=True)
                            finally:
                                if pdf_response is not None:
                                    pdf_response.close()

                    if "text/html" in content_type.lower() and (urlparse(final_url).hostname or "").lower() == "ieeexplore.ieee.org":
                        arnumber = extract_ieee_arnumber(final_url)
                        if arnumber:
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
                                                score = title_match_score(pdf_title, expected)
                                                if score >= float(verify_title_threshold):
                                                    item.download_status = "downloaded_pdf"
                                                    item.downloaded_file = out_file.name
                                                    item.note = stamp_final_url
                                                    name = sanitize_filename_component(pdf_title or expected)
                                                    if name:
                                                        renamed = unique_path(downloads_dir / f"{prefix} {name}.pdf")
                                                        if renamed.name != out_file.name:
                                                            out_file.replace(renamed)
                                                            out_file = renamed
                                                            item.downloaded_file = out_file.name
                                                    out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
                                                    item.downloaded_file = rel_path
                                                    item.note = f"{stamp_final_url} | title_match={score:.3f}"
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
                                                    return
                                                mismatch_file = unique_path(downloads_dir / f"{prefix}__mismatch.pdf")
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
                                                        error=f"score={score:.3f}; pdf_title={pdf_title[:140]}",
                                                    )
                                                )
                                                break

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
                                            return
                                        finally:
                                            if tmp_file.exists():
                                                tmp_file.unlink(missing_ok=True)

                                    html_bytes = bytearray()
                                    if pdf_first_chunk:
                                        html_bytes.extend(pdf_first_chunk[:1024 * 1024])
                                    limit = 1024 * 1024 * 2
                                    for chunk in pdf_chunks:
                                        if not chunk:
                                            continue
                                        remaining = limit - len(html_bytes)
                                        if remaining <= 0:
                                            break
                                        html_bytes.extend(chunk[:remaining])
                                        if len(html_bytes) >= limit:
                                            break
                                    html_text = html_bytes.decode("utf-8", errors="ignore")
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
                                            if direct_first and is_probably_pdf(direct_first):
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
                                                        score = title_match_score(pdf_title, expected)
                                                        if score >= float(verify_title_threshold):
                                                            item.download_status = "downloaded_pdf"
                                                            item.downloaded_file = out_file.name
                                                            item.note = final_pdf_url
                                                            name = sanitize_filename_component(pdf_title or expected)
                                                            if name:
                                                                renamed = unique_path(downloads_dir / f"{prefix} {name}.pdf")
                                                                if renamed.name != out_file.name:
                                                                    out_file.replace(renamed)
                                                                    out_file = renamed
                                                                    item.downloaded_file = out_file.name
                                                            out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
                                                            item.downloaded_file = rel_path
                                                            item.note = f"{final_pdf_url} | title_match={score:.3f}"
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
                                                            return
                                                        mismatch_file = unique_path(downloads_dir / f"{prefix}__mismatch.pdf")
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
                                                                error=f"score={score:.3f}; pdf_title={pdf_title[:140]}",
                                                            )
                                                        )
                                                    else:
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
                                                        return
                                                finally:
                                                    if tmp_file.exists():
                                                        tmp_file.unlink(missing_ok=True)
                                        finally:
                                            if direct_response is not None:
                                                direct_response.close()
                                finally:
                                    if pdf_response is not None:
                                        pdf_response.close()

                    landing_file = downloads_dir / f"{prefix}_landing.url.txt"
                    landing_file.write_text(final_url + "\n", encoding="utf-8")
                    item.download_status = "saved_landing_url"
                    item.downloaded_file = landing_file.name
                    item.note = final_url
                    logger.add(
                        DownloadAttempt(
                            phase=phase,
                            ref_number=item.number,
                            candidate_url=candidate,
                            final_url=final_url,
                            status_code=int(response.status_code),
                            content_type=content_type,
                            outcome="saved_landing_url",
                            waited_seconds=0.0,
                            error="",
                        )
                    )
                    return
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
                time.sleep(waited_s)
                continue

    item.download_status = "failed"
    item.note = "No reachable URL/DOI PDF or landing page."


def run_initial_download_phase(
    refs: list[ReferenceItem],
    downloads_dir: Path,
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
    verified_dir: Path | None,
) -> None:
    """
    初次下载阶段：并发尝试每条参考文献的候选链接。

    关键点：
    - 使用 thread_local 为每个线程保存一个 Session，实现连接复用；
    - show_progress 为 True 且安装 tqdm 时，会显示进度条。
    """
    if not refs:
        return

    thread_local = threading.local()
    domain_limiter = DomainLimiter(max_per_domain, min_delay_ms=min_domain_delay_ms)

    def worker(item: ReferenceItem) -> None:
        # 每个线程只初始化一次 Session，降低 TLS 握手与连接创建开销。
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session(pool_size=max(8, workers * 2), user_agent=user_agent, cookies_jar=cookies_jar)
        try_download(
            session=thread_local.session,
            item=item,
            downloads_dir=downloads_dir,
            timeout=timeout,
            retries=retries,
            use_doi=use_doi,
            max_candidates_per_item=max_candidates_per_item,
            domain_limiter=domain_limiter,
            logger=logger,
            phase="initial",
            verify_title_rename=verify_title_rename,
            verify_title_threshold=verify_title_threshold,
            verified_dir=verified_dir,
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
    verified_dir: Path | None,
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

    def worker(item: ReferenceItem) -> None:
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session(pool_size=max(8, workers * 2), user_agent=user_agent, cookies_jar=cookies_jar)
        session = thread_local.session
        secondary_dois, secondary_urls = lookup_secondary_ranked(session, item=item, timeout=lookup_timeout, top_k=secondary_top_k)
        item.dois = unique_preserve_order(list(item.dois) + list(secondary_dois))
        item.urls = unique_preserve_order(list(item.urls) + list(secondary_urls))
        if item.dois or item.urls:
            try_download(
                session=session,
                item=item,
                downloads_dir=downloads_dir,
                timeout=timeout,
                retries=retries,
                use_doi=True,
                max_candidates_per_item=max_candidates_per_item,
                domain_limiter=domain_limiter,
                logger=logger,
                phase="secondary",
                verify_title_rename=verify_title_rename,
                verify_title_threshold=verify_title_threshold,
                verified_dir=verified_dir,
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
        default="pypdf",
        help="PDF text parser backend (default: pypdf)",
    )
    parser.add_argument("--header-margin", type=float, default=40.0, help="Top margin for pdfplumber crop")
    parser.add_argument("--footer-margin", type=float, default=40.0, help="Bottom margin for pdfplumber crop")

    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds")
    parser.add_argument("--lookup-timeout", type=int, default=6, help="Secondary lookup API timeout seconds")
    parser.add_argument("--retries", type=int, default=1, help="Retries per candidate URL")
    parser.add_argument("--cookies", help="cookies.txt (Netscape) path for authenticated downloads")
    parser.add_argument("--verify-title-rename", action="store_true", help="Verify downloaded PDF title and rename on match")
    parser.add_argument("--verify-title-threshold", type=float, default=0.55, help="Title match threshold (Jaccard)")
    parser.add_argument("--verified-subdir", default="verified_pdfs", help="Put verified PDFs into downloads/<subdir> (empty disables)")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent worker count")
    parser.add_argument("--max-per-domain", type=int, default=2, help="Max concurrent requests per domain (0 means unlimited)")
    parser.add_argument("--min-domain-delay-ms", type=int, default=0, help="Minimum delay per domain between requests")
    parser.add_argument("--user-agent", default="ReferenceDownloader/1.1", help="HTTP User-Agent header")
    parser.add_argument("--download-log", default="download_log.csv", help="Write download attempt log CSV (empty to disable)")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bars")

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
    parser.add_argument("--no-download", action="store_true", help="Only extract and number references")
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
    verified_dir: Path | None = None
    if bool(args.verify_title_rename) and str(getattr(args, "verified_subdir", "")).strip():
        verified_dir = downloads_dir / str(args.verified_subdir).strip()

    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF does not exist: {input_pdf}")

    # 1) 读取全文文本（不同后端对页眉页脚/分栏的抗噪能力不同）
    full_text = read_pdf_text(
        input_pdf,
        parser=args.pdf_parser,
        header_margin=args.header_margin,
        footer_margin=args.footer_margin,
    )
    # 2) 截取参考文献章节，并按风格分段成条目
    ref_section = extract_references_section(full_text)
    refs = split_references(ref_section)

    output_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    if not args.no_download:
        logger = DownloadLogger()
        initial_refs = refs[: args.download_max] if args.download_max > 0 else refs
        run_initial_download_phase(
            initial_refs,
            downloads_dir=downloads_dir,
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
            verified_dir=verified_dir,
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
                verified_dir=verified_dir,
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


if __name__ == "__main__":
    main()
