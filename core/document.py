"""Conservative document-kind evidence. Unknown/scanned is review, not failure."""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from core.pdf_files import PdfReader

VERIFICATION_VERSION = 2


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(text or ""))
    return text.replace("\u00ad", "").replace("\u00a0", " ")


def front_matter(text: str) -> str:
    # Never use a cited title/DOI in the bibliography as the paper's identity.
    return re.split(r"(?im)^\s*(?:\d+[. ]+)?(?:references|bibliography|参考文献)\s*$", text, maxsplit=1)[0]


@dataclass
class DocumentEvidence:
    title: str
    page_texts: list[str]
    page_count: int
    kind: str
    reason: str
    metadata_text: str = ""


def read_document(path: Path, *, reader_cls=PdfReader, source_url: str = "") -> DocumentEvidence:
    try:
        reader = reader_cls(str(path))
        if getattr(reader, "is_encrypted", False) and not reader.decrypt(""):
            return DocumentEvidence("", [], 0, "unreadable", "encrypted_pdf")
        meta = getattr(reader, "metadata", None) or {}
        title = getattr(meta, "title", None) or (meta.get("/Title", "") if hasattr(meta, "get") else "")
        metadata_text = "\n".join(str(meta.get(k, "")) for k in ("/Title", "/Subject", "/Keywords")) if hasattr(meta, "get") else str(title)
        pages = getattr(reader, "pages", [])
        count = len(pages)
        texts = []
        for page in list(pages[:3]):
            try:
                texts.append(clean_text(page.extract_text() or "")[:40000])
            except Exception:
                texts.append("")
        title = clean_text(str(title or ""))
        combined = "\n".join(texts)
        # Limit artifact labels to the front; references to SI in the body are normal.
        heading = title + "\n" + (texts[0][:1800] if texts else "")
        title_area = title + "\n" + re.split(r"(?im)^\s*(?:abstract|摘要|[IVX\d. ]*introduction)\b", texts[0] if texts else "", maxsplit=1)[0][:1500]
        name = unquote(Path(urlparse(source_url).path).name).lower()
        if re.search(r"(?im)^\s*(?:electronic\s+)?(?:supporting information|supplementary (?:material|information|data)|supplemental material)\b", title_area) or re.search(r"(?:^|[_-])(?:suppl?\d*|esm\d*|mmc\d+)(?:[_.-]|$)", name):
            kind, reason = "supplementary", "supporting_material"
        elif re.search(r"(?i)\b(?:this is (?:a |the )?preview|preview (?:of|copy)|purchase (?:the )?full text|sample (?:chapter|pages))\b", heading):
            kind, reason = "preview", "preview_not_fulltext"
        elif re.search(r"(?im)^\s*(?:abstract only|conference abstract|extended abstract)\b", heading):
            kind, reason = "abstract", "abstract_not_fulltext"
        elif count == 0:
            kind, reason = "unreadable", "no_pages"
        elif len(re.sub(r"\s", "", combined)) < 80:
            kind, reason = "unknown", "no_extractable_text_scan_or_blank"
        elif count >= 3 and len(combined) >= 200 and ("powerpoint" in title.lower() or re.search(r"(?im)^\s*(?:outline|agenda)\s*$", combined)):
            kind, reason = "presentation", "multi_page_presentation"
        elif (len(combined) >= 200 and re.search(r"(?im)^\s*(?:[IVX\d]+[.)]?\s*)?(?:introduction|methods?|results|conclusion|technical specifications|claims|引言|绪论|结论)\b", combined)) or (count >= 2 and len(combined) >= 1200) or (count == 1 and len(combined) >= 1500):
            kind, reason = "full_text", "main_text_evidence"
        else:
            kind, reason = "unknown", "insufficient_main_text_evidence"
        return DocumentEvidence(title, texts, count, kind, reason, clean_text(metadata_text))
    except Exception as exc:
        return DocumentEvidence("", [], 0, "unreadable", f"parse_error:{type(exc).__name__}")
