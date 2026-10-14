from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from difflib import SequenceMatcher
from core.document import clean_text, front_matter, read_document
from core.pdf_files import move_without_overwrite

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    from PyPDF2 import PdfReader  # type: ignore


def sanitize_filename_component(text: str, max_len: int = 90) -> str:
    s = clean_text(text).strip()
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
    dest = move_without_overwrite(out_file, verified_dir / out_file.name)
    rel = dest.relative_to(downloads_dir).as_posix()
    return dest, rel


RenameMode = Literal["original", "number_only", "number_and_original"]


def build_verified_pdf_name(*, prefix: str, original_name: str, rename_mode: RenameMode) -> str:
    clean = sanitize_filename_component(original_name)
    mode = str(rename_mode or "number_and_original").strip().lower()
    if mode == "original":
        return f"{clean}.pdf" if clean else f"{prefix}.pdf"
    if mode == "number_only":
        return f"{prefix}.pdf"
    # default: number + original
    return f"{prefix} {clean}.pdf" if clean else f"{prefix}.pdf"


# ── CJK title matching support ────────────────────────────────────────────

_CJK_RANGE = r"一-鿿㐀-䶿"

# Single CJK characters that carry little semantic weight in titles
_CJK_STOP_CHARS = set(
    "的了吗我你是他有在也都会这不让与及或其之到可以中对和而但被从把所因为自就个以能下去里过后说些那还又如它才用"
    "日年月至大小高低温正负重新旧前后内外部件器等体量测检实分制控保护处存运输按选择开关连"
)

# Common CJK academic bigrams that function like English stop words
_CJK_STOP_BIGRAMS = {
    "基于", "用于", "一种", "方法", "研究", "及其", "本文", "提出", "进行", "通过",
    "利用", "实现", "采用", "分析", "设计", "结果", "表明", "验证", "仿真", "实验",
    "系统", "模型", "问题", "应用", "影响", "不同", "之间", "具有", "考虑", "针对",
    "比较", "主要", "能够", "可以", "其中", "所示", "如图", "上述", "以下", "相关",
    "改进", "优化", "性能", "特性", "控制", "建模", "估计", "预测", "监测", "检测",
}


def normalize_title_tokens(text: str) -> list[str]:
    """Tokenize a title for Jaccard matching, preserving CJK characters.

    ASCII words shorter than 3 characters are dropped (unchanged from the
    original behaviour).  CJK titles are decomposed into character bigrams
    so that ``title_match_score`` works for Chinese-only, English-only, and
    mixed-language titles.
    """
    raw = clean_text(text).lower()
    raw = re.sub(r"[‐-―−]", "-", raw)
    raw = re.sub(rf"[^a-z0-9{_CJK_RANGE}]+", " ", raw)

    tokens: list[str] = []
    for t in raw.split():
        if re.match(r"^[a-z0-9]+$", t):
            if len(t) >= 3:
                tokens.append(t)
        else:
            tokens.append(t)

    stop = {
        "the", "and", "for", "with", "from", "into", "over", "under",
        "between", "within", "using", "use", "via", "based",
        "model", "models", "analysis", "study", "method", "methods",
        "approach", "approaches", "system", "systems", "paper", "review",
    }
    tokens = [t for t in tokens if t not in stop]

    cjk_tokens: list[str] = []
    ascii_tokens: list[str] = []
    for t in tokens:
        cjk_part = "".join(re.findall(rf"[{_CJK_RANGE}]+", t))
        ascii_part = "".join(re.findall(r"[a-z0-9]+", t))
        if cjk_part:
            cjk_tokens.append(cjk_part)
        if ascii_part and len(ascii_part) >= 3:
            ascii_tokens.append(ascii_part)

    if not cjk_tokens:
        return ascii_tokens

    result: list[str] = list(ascii_tokens)
    joined = "".join(cjk_tokens)
    chars = list(joined)
    for i in range(len(chars) - 1):
        a, b = chars[i], chars[i + 1]
        bigram = a + b
        if bigram in _CJK_STOP_BIGRAMS:
            continue
        if a in _CJK_STOP_CHARS and b in _CJK_STOP_CHARS:
            continue
        result.append(bigram)
    return result


def title_match_score(pdf_title: str, expected_title: str) -> float:
    compact = lambda text: re.sub(r"[^a-z0-9一-鿿]", "", clean_text(text).casefold())
    left, right = compact(pdf_title), compact(expected_title)
    if len(left) >= 16 and left == right:
        return 1.0
    a = set(normalize_title_tokens(pdf_title))
    b = set(normalize_title_tokens(expected_title))
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union <= 0:
        return 0.0
    return float(inter) / float(union)


def _extract_pdf_title_from_reader(reader: Any) -> str | None:
    def clean_line(line: str) -> str:
        s = (line or "").strip()
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def is_bad_title_line(s: str) -> bool:
        low = s.lower()
        if not (12 <= len(s) <= 240):
            return True
        if "doi.org/" in low or low.startswith("http"):
            return True
        if low.startswith("arxiv") or low.startswith("preprint"):
            return True
        if re.search(r"\b(vol|no|pp|issue|pages?)\b", low):
            return True
        if re.search(r"^(ieee|science china|springer|elsevier|acm|sciencedirect)\b", low):
            return True
        if re.fullmatch(r"[A-Z0-9 .,:;()\\-]{16,}", s):
            return True
        return False

    meta = getattr(reader, "metadata", None)
    title = None
    if meta is not None:
        title = getattr(meta, "title", None)
        if not title and isinstance(meta, dict):
            title = meta.get("/Title") or meta.get("Title")
    if isinstance(title, str) and title.strip():
        return title.strip()

    pages = getattr(reader, "pages", None)
    if pages:
        text = (pages[0].extract_text() or "").strip()
        if text:
            lines = [clean_line(l) for l in text.splitlines() if clean_line(l)]
            candidates = [l for l in lines[:60] if not is_bad_title_line(l)]
            if candidates:
                return max(candidates, key=len)
    return None


def extract_pdf_title_from_file(pdf_path: Path, *, reader_cls=PdfReader) -> str | None:
    try:
        reader = reader_cls(str(pdf_path))
        return _extract_pdf_title_from_reader(reader)
    except Exception:
        return None


def extract_pdf_first_page_text(pdf_path: Path, *, max_chars: int = 12000, reader_cls=PdfReader) -> str:
    try:
        reader = reader_cls(str(pdf_path))
        pages = getattr(reader, "pages", None)
        if not pages:
            return ""
        text = (pages[0].extract_text() or "").strip()
        if not text:
            return ""
        if len(text) > max_chars:
            return text[:max_chars]
        return text
    except Exception:
        return ""


def extract_pdf_best_line_score(pdf_path: Path, expected_title: str, *, reader_cls=PdfReader) -> tuple[float, str]:
    try:
        reader = reader_cls(str(pdf_path))
        pages = getattr(reader, "pages", None)
        if not pages:
            return 0.0, ""
        text = (pages[0].extract_text() or "").strip()
        if not text:
            return 0.0, ""
        best_score = 0.0
        best_line = ""
        for raw in text.splitlines()[:80]:
            line = re.sub(r"\s+", " ", (raw or "").strip())
            if not (12 <= len(line) <= 240):
                continue
            s = title_match_score(line, expected_title)
            if s > best_score:
                best_score = s
                best_line = line
        return best_score, best_line
    except Exception:
        return 0.0, ""


@dataclass
class VerifyWeights:
    title_weight: float = 1.0
    line_weight: float = 1.0
    year_hit_bonus: float = 0.0
    year_miss_multiplier: float = 0.95
    author_hit_bonus: float = 0.0
    author_miss_multiplier: float = 0.97


def coerce_verify_weights(value) -> VerifyWeights:
    if isinstance(value, VerifyWeights):
        return value
    if isinstance(value, dict):
        return VerifyWeights(
            title_weight=float(value.get("title_weight", 1.0)),
            line_weight=float(value.get("line_weight", 1.0)),
            year_hit_bonus=float(value.get("year_hit_bonus", 0.0)),
            year_miss_multiplier=float(value.get("year_miss_multiplier", 0.95)),
            author_hit_bonus=float(value.get("author_hit_bonus", 0.0)),
            author_miss_multiplier=float(value.get("author_miss_multiplier", 0.97)),
        )
    return VerifyWeights()


def compute_verify_score(
    *,
    title_score: float,
    line_score: float,
    year_present: bool,
    year_hit: bool,
    author_present: bool,
    author_hit: bool,
    weights: VerifyWeights,
) -> float:
    base = max(float(title_score) * float(weights.title_weight), float(line_score) * float(weights.line_weight))
    score = float(base)
    if year_present:
        if year_hit:
            score += float(weights.year_hit_bonus)
        else:
            score *= float(weights.year_miss_multiplier)
    if author_present:
        if author_hit:
            score += float(weights.author_hit_bonus)
        else:
            score *= float(weights.author_miss_multiplier)
    return float(score)


@dataclass
class VerifyDecision:
    outcome: Literal["downloaded_pdf", "pdf_title_mismatch", "needs_review", "non_main_pdf"]
    score: float
    title_score: float
    line_score: float
    year_hit: bool
    author_hit: bool
    pdf_title: str
    best_line: str
    file_path: Path
    rel_path: str
    document_kind: str = "unknown"
    reason: str = ""
    identity_match: str = ""


def _best_title_lines(page_texts: list[str], expected: str) -> tuple[float, str]:
    best_score, best_line = 0.0, ""
    if page_texts and (len(page_texts[0]) >= 1500 or re.search(r"(?im)^\s*(?:abstract|摘要|[IVX\d. ]*introduction)\b", page_texts[0])):
        page_texts = page_texts[:1]
    for text in page_texts:
        text = front_matter(text)
        text = re.sub(r"(?<=\w)\s*\n\s*[-‐‑]\s*\n\s*(?=\w)", "-", text)
        text = re.split(r"(?im)^\s*(?:abstract|[IVX\d. ]*introduction|摘要)\b", text, maxsplit=1)[0]
        lines = [re.sub(r"\s+", " ", clean_text(line)).strip() for line in text.splitlines()[:80]]
        lines = [line for line in lines if line]
        for start in range(len(lines)):
            for width in range(1, 5):
                line = " ".join(lines[start:start + width])
                if not 12 <= len(line) <= 360:
                    continue
                # Keep the semantic hyphen, joining a wrap without an extra space.
                line = re.sub(r"([-‐‑])\s+(?=\w)", r"\1", line)
                score = title_match_score(line, expected)
                if score > best_score:
                    best_score, best_line = score, line
    return best_score, best_line


def assess_pdf(*, out_file: Path, expected_title: str, ref_year: int | None,
               surname: str, verify_title_threshold: float, verify_weights,
               expected_dois: list[str] | None = None, expected_identifiers: list[str] | None = None,
               source_url: str = "", check_identity: bool = True, expected_kind: str = "",
               reader_cls=PdfReader) -> VerifyDecision:
    evidence = read_document(out_file, reader_cls=reader_cls, source_url=source_url)
    title_score = title_match_score(evidence.title, expected_title)
    line_score, best_line = _best_title_lines(evidence.page_texts, expected_title)
    page_text = front_matter("\n".join(evidence.page_texts)).lower()
    year_hit = bool(ref_year) and str(ref_year) in page_text
    author_hit = bool(surname) and surname.lower() in page_text
    score = compute_verify_score(title_score=title_score, line_score=line_score,
        year_present=bool(ref_year), year_hit=year_hit, author_present=bool(surname),
        author_hit=author_hit, weights=coerce_verify_weights(verify_weights))
    first_page = front_matter(evidence.page_texts[0] if evidence.page_texts else "")
    primary_lines = [line for line in first_page.splitlines() if
        re.search(r"(?i)^\s*(?:doi\s*:|https?://(?:dx\.)?doi\.org/)|digital object identifier", line)]
    identity_text = evidence.metadata_text + "\n" + "\n".join(primary_lines)
    found_dois = {d.rstrip(".,;)").casefold() for d in re.findall(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", identity_text)}
    wanted = {re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", d.strip(), flags=re.I).casefold().rstrip(".,;)") for d in (expected_dois or [])}
    doi_hit = bool(wanted & found_dois)
    def patent_ids(text):
        values = set()
        for country, number, kind, digit in re.findall(r"\b(US|EP|WO|AT|CN)\s*([0-9][0-9 ,./-]{3,24})\s*([ABC])\s*(\d?)", text, flags=re.I):
            number = re.sub(r"\D", "", number).lstrip("0")
            values.add((country.upper(), number, kind.upper(), digit))
        return values
    wanted_patents = patent_ids("\n".join(expected_identifiers or []))
    # Patent bibliographies do not establish the patent's own publication number.
    found_patents = patent_ids(evidence.title)
    if not found_patents:
        first_patent = re.search(r"\b(?:US|EP|WO|AT|CN)\s*[0-9][0-9 ,./-]{3,24}\s*[ABC]\s*\d?", first_page[:600], flags=re.I)
        found_patents = patent_ids(first_patent.group()) if first_patent else set()
    patent_hit = bool(wanted_patents & found_patents)
    identity = "doi" if doi_hit else "identifier" if patent_hit else "title" if score >= verify_title_threshold else ""
    # A near-spelling title needs independent author/year evidence, and cannot
    # change numeric ratings. This is not a lower global title threshold.
    compact = lambda text: re.sub(r"[^a-z0-9一-鿿]", "", clean_text(text).casefold())
    independent = (bool(ref_year) or bool(surname)) and (not ref_year or year_hit) and (not surname or author_hit)
    if not identity and independent:
        for candidate in (evidence.title, best_line):
            a, b = compact(candidate), compact(expected_title)
            if min(len(a), len(b)) >= 24 and re.findall(r"\d+", a) == re.findall(r"\d+", b) and SequenceMatcher(None, a, b).ratio() >= .96:
                identity = "title_spelling_with_author_year"
                break
    conflict = (bool(wanted) and bool(found_dois) and not doi_hit) or (bool(wanted_patents) and bool(found_patents) and not patent_hit)
    chosen_title = evidence.title if title_score >= line_score else best_line
    chosen_title = re.sub(r"^\s*(?:[IVX]+(?:\.\d+)+\.?|\d+(?:\.\d+)*[.)])\s+", "", chosen_title)
    expected_numbers = re.findall(r"\d+(?:\.\d+)?", expected_title)
    actual_numbers = re.findall(r"\d+(?:\.\d+)?", chosen_title)
    numeric_conflict = identity not in {"doi", "identifier"} and bool(expected_numbers and actual_numbers) and expected_numbers != actual_numbers
    if evidence.kind in {"supplementary", "preview", "abstract"}:
        outcome, reason = "non_main_pdf", evidence.reason
    elif evidence.kind in {"unknown", "unreadable"}:
        outcome, reason = "needs_review", evidence.reason
    elif evidence.kind == "presentation" and expected_kind != "presentation":
        outcome, reason = "needs_review", "presentation_not_expected_for_article"
    elif conflict and check_identity:
        outcome, reason = "needs_review", "identifier_conflict"
    elif numeric_conflict and check_identity:
        outcome, reason = "needs_review", "title_numeric_conflict"
    elif not identity and check_identity and (not evidence.title.strip() or evidence.title.casefold() in {"powerpoint presentation", "microsoft word", "untitled", "unknown"}):
        outcome, reason = "needs_review", "identity_not_extractable"
    elif not check_identity or identity:
        outcome, reason = "downloaded_pdf", evidence.reason
    else:
        outcome, reason = "pdf_title_mismatch", "identity_not_confirmed"
    return VerifyDecision(outcome, float(score), float(title_score), float(line_score),
        bool(year_hit), bool(author_hit), evidence.title, best_line, out_file, out_file.name,
        evidence.kind, reason, identity)


def verify_and_rename_pdf(
    *,
    prefix: str,
    out_file: Path,
    downloads_dir: Path,
    verified_dir: Path | None,
    mismatch_dir: Path | None,
    expected_title: str,
    ref_year: int | None,
    surname: str,
    verify_title_threshold: float,
    verify_weights,
    verify_rename_mode: RenameMode = "number_and_original",
    reader_cls=PdfReader,
    expected_dois: list[str] | None = None,
    expected_identifiers: list[str] | None = None,
    source_url: str = "",
    check_identity: bool = True,
    expected_kind: str = "",
) -> VerifyDecision:
    decision = assess_pdf(out_file=out_file, expected_title=expected_title, ref_year=ref_year,
        surname=surname, verify_title_threshold=verify_title_threshold, verify_weights=verify_weights,
        expected_dois=expected_dois, expected_identifiers=expected_identifiers, source_url=source_url,
        check_identity=check_identity, expected_kind=expected_kind, reader_cls=reader_cls)
    if decision.outcome == "downloaded_pdf":
        # A known reference title is more trustworthy than a PDF producer's metadata.
        name_source = expected_title or decision.best_line or decision.pdf_title
        target_name = build_verified_pdf_name(prefix=prefix, original_name=(name_source or expected_title), rename_mode=verify_rename_mode)
        desired = downloads_dir / target_name
        if desired.resolve() != out_file.resolve():
            out_file = move_without_overwrite(out_file, desired)
        out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
    else:
        folder = (downloads_dir / "review_pdfs") if decision.outcome == "needs_review" else (mismatch_dir or downloads_dir)
        tag = "review" if decision.outcome == "needs_review" else "non_main" if decision.outcome == "non_main_pdf" else "mismatch"
        out_file = move_without_overwrite(out_file, folder / f"{prefix}__{tag}.pdf")
        rel_path = out_file.relative_to(downloads_dir).as_posix()
    decision.file_path, decision.rel_path = out_file, rel_path
    return decision
