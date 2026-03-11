from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    from PyPDF2 import PdfReader  # type: ignore


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
    outcome: Literal["downloaded_pdf", "pdf_title_mismatch"]
    score: float
    title_score: float
    line_score: float
    year_hit: bool
    author_hit: bool
    pdf_title: str
    best_line: str
    file_path: Path
    rel_path: str


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
) -> VerifyDecision:
    pdf_title = extract_pdf_title_from_file(out_file, reader_cls=reader_cls) or ""
    title_score = title_match_score(pdf_title, expected_title)
    line_score, best_line = extract_pdf_best_line_score(out_file, expected_title, reader_cls=reader_cls)
    page_text = extract_pdf_first_page_text(out_file, reader_cls=reader_cls).lower()
    year_hit = bool(ref_year) and str(ref_year) in page_text
    author_hit = bool(surname) and surname in page_text

    weights = coerce_verify_weights(verify_weights)
    score = compute_verify_score(
        title_score=title_score,
        line_score=line_score,
        year_present=bool(ref_year),
        year_hit=bool(year_hit),
        author_present=bool(surname),
        author_hit=bool(author_hit),
        weights=weights,
    )

    if score >= float(verify_title_threshold):
        name_source = best_line if (line_score * float(weights.line_weight)) > (title_score * float(weights.title_weight)) and best_line else pdf_title
        target_name = build_verified_pdf_name(prefix=prefix, original_name=(name_source or expected_title), rename_mode=verify_rename_mode)
        desired = downloads_dir / target_name
        if desired.resolve() != out_file.resolve():
            renamed = unique_path(desired)
            if renamed.name != out_file.name:
                out_file.replace(renamed)
                out_file = renamed
        out_file, rel_path = move_verified_pdf(out_file, downloads_dir=downloads_dir, verified_dir=verified_dir)
        return VerifyDecision(
            outcome="downloaded_pdf",
            score=float(score),
            title_score=float(title_score),
            line_score=float(line_score),
            year_hit=bool(year_hit),
            author_hit=bool(author_hit),
            pdf_title=pdf_title,
            best_line=best_line,
            file_path=out_file,
            rel_path=rel_path,
        )

    mismatch_file = unique_path((mismatch_dir or downloads_dir) / f"{prefix}__mismatch.pdf")
    out_file.replace(mismatch_file)
    rel_path = mismatch_file.relative_to(downloads_dir).as_posix() if mismatch_file.is_relative_to(downloads_dir) else mismatch_file.name
    return VerifyDecision(
        outcome="pdf_title_mismatch",
        score=float(score),
        title_score=float(title_score),
        line_score=float(line_score),
        year_hit=bool(year_hit),
        author_hit=bool(author_hit),
        pdf_title=pdf_title,
        best_line=best_line,
        file_path=mismatch_file,
        rel_path=rel_path,
    )
