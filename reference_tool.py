#!/usr/bin/env python3
"""
Extract, number, and download references from a paper PDF.

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
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

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

NUMERIC_REF_RE = re.compile(
    r"(?ms)^\s*(?:\[(\d+)\]|(\d+)[\.\)]|[\(（](\d+)[\)）])\s+(.*?)(?=^\s*(?:\[\d+\]|\d+[\.\)]|[\(（]\d+[\)）])\s+|\Z)"
)

REF_HEADING_RE = re.compile(
    r"(?im)^\s*(references|bibliography|works cited|reference list|参考文献)\s*$"
)
REF_END_RE = re.compile(
    r"(?im)^\s*(appendix|appendices|acknowledg(e)?ments?|about the authors?)\b"
)
AUTHOR_YEAR_START_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\- ]{0,40},\s+.+\((?:19|20)\d{2}[a-z]?\)"
)
MLA_LIKE_START_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\- ]{0,40},\s+.+\.\s+.+"
)


@dataclass
class ReferenceItem:
    number: int
    text: str
    dois: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    download_status: str = "not_attempted"
    downloaded_file: str = ""
    note: str = ""


def make_session(pool_size: int, user_agent: str) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": user_agent})
    return session


def read_pdf_text_pypdf(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def read_pdf_text_pdfplumber(pdf_path: Path, header_margin: float, footer_margin: float) -> str:
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
    if parser == "pdfplumber":
        return read_pdf_text_pdfplumber(pdf_path, header_margin=header_margin, footer_margin=footer_margin)
    return read_pdf_text_pypdf(pdf_path)


def cleanup_reference_text(text: str) -> str:
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_references_section(full_text: str) -> str:
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
    if AUTHOR_YEAR_START_RE.match(line):
        return True
    if MLA_LIKE_START_RE.match(line):
        return True
    # Another common pattern: "Author et al., 2021, ..."
    if re.match(r"^[A-Z].+et al\.,\s*(?:19|20)\d{2}", line):
        return True
    return False


def parse_non_numeric_references(ref_section_text: str) -> list[ReferenceItem]:
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
    numeric = parse_numeric_references(ref_section_text)
    if len(numeric) >= 3:
        return numeric
    non_numeric = parse_non_numeric_references(ref_section_text)
    if non_numeric:
        return non_numeric
    raise ValueError("Unable to parse references from section.")


def guess_title_query(ref_text: str) -> str:
    tmp = re.sub(r"['\"“”‘’]", "", ref_text)
    parts = [p.strip() for p in re.split(r"[.;。；]", tmp) if p.strip()]
    if not parts:
        return ref_text[:120]
    best = max(parts, key=len)
    best = re.sub(r"\b(?:vol|no|pp|ed|dept|univ|university)\b.*$", "", best, flags=re.IGNORECASE)
    return best[:180].strip()


def lookup_crossref_by_bibliographic(
    session: requests.Session,
    item: ReferenceItem,
    timeout: int,
) -> tuple[list[str], list[str]]:
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
    for url in item.urls:
        yield url
    if use_doi:
        for doi in item.dois:
            yield f"https://doi.org/{quote(doi, safe=':/')}"


def try_download(
    session: requests.Session,
    item: ReferenceItem,
    downloads_dir: Path,
    timeout: int,
    retries: int,
    use_doi: bool,
    max_candidates_per_item: int,
) -> None:
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

        for _ in range(max(1, retries)):
            try:
                response = session.get(
                    candidate,
                    timeout=timeout,
                    stream=True,
                    allow_redirects=True,
                )
                if not response.ok:
                    continue
                final_url = response.url or candidate
                ctype = (response.headers.get("content-type") or "").lower()
                is_pdf = "application/pdf" in ctype or final_url.lower().endswith(".pdf")

                if is_pdf:
                    out_file = downloads_dir / f"{prefix}.pdf"
                    with out_file.open("wb") as f:
                        for chunk in response.iter_content(chunk_size=1024 * 64):
                            if chunk:
                                f.write(chunk)
                    item.download_status = "downloaded_pdf"
                    item.downloaded_file = out_file.name
                    item.note = final_url
                    return

                landing_file = downloads_dir / f"{prefix}_landing.url.txt"
                landing_file.write_text(final_url + "\n", encoding="utf-8")
                item.download_status = "saved_landing_url"
                item.downloaded_file = landing_file.name
                item.note = final_url
                return
            except requests.RequestException:
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
) -> None:
    if not refs:
        return

    thread_local = threading.local()

    def worker(item: ReferenceItem) -> None:
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session(pool_size=max(8, workers * 2), user_agent="ReferenceDownloader/1.1")
        try_download(
            session=thread_local.session,
            item=item,
            downloads_dir=downloads_dir,
            timeout=timeout,
            retries=retries,
            use_doi=use_doi,
            max_candidates_per_item=max_candidates_per_item,
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
    workers: int,
    show_progress: bool,
) -> None:
    failed = [r for r in refs if r.download_status == "failed"]
    if max_items > 0:
        failed = failed[:max_items]
    if not failed:
        return

    thread_local = threading.local()

    def worker(item: ReferenceItem) -> None:
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session(pool_size=max(8, workers * 2), user_agent="ReferenceDownloader/1.1")
        session = thread_local.session
        crossref_dois, crossref_urls = lookup_crossref_by_bibliographic(session, item=item, timeout=lookup_timeout)
        openalex_dois, openalex_urls = lookup_openalex(session, item=item, timeout=lookup_timeout)
        item.dois = sorted(set(item.dois + crossref_dois + openalex_dois))
        item.urls = sorted(set(item.urls + crossref_urls + openalex_urls))
        if item.dois or item.urls:
            try_download(
                session=session,
                item=item,
                downloads_dir=downloads_dir,
                timeout=timeout,
                retries=retries,
                use_doi=True,
                max_candidates_per_item=max_candidates_per_item,
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
    parser = argparse.ArgumentParser(
        description="Extract references from PDF, number them, and download where possible."
    )
    parser.add_argument("--input", "-i", required=True, help="Input paper PDF path")
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
    parser.add_argument("--workers", type=int, default=8, help="Concurrent worker count")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bars")

    parser.add_argument(
        "--max-candidates-per-item",
        type=int,
        default=3,
        help="Max URLs tried per item (0 means unlimited).",
    )
    parser.add_argument("--skip-doi", action="store_true", help="Skip DOI URL attempts in initial phase")
    parser.add_argument("--initial-max", type=int, default=0, help="Initial phase max items (0 means all)")

    parser.add_argument(
        "--secondary-lookup",
        action="store_true",
        help="For failed items, query Crossref/OpenAlex and retry.",
    )
    parser.add_argument("--secondary-max", type=int, default=40, help="Secondary phase max failed items")
    parser.add_argument("--no-download", action="store_true", help="Only extract and number references")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    input_pdf = Path(args.input)
    output_dir = Path(args.output)
    downloads_dir = output_dir / "downloads"

    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF does not exist: {input_pdf}")

    full_text = read_pdf_text(
        input_pdf,
        parser=args.pdf_parser,
        header_margin=args.header_margin,
        footer_margin=args.footer_margin,
    )
    ref_section = extract_references_section(full_text)
    refs = split_references(ref_section)

    output_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    if not args.no_download:
        initial_refs = refs[: args.initial_max] if args.initial_max > 0 else refs
        run_initial_download_phase(
            initial_refs,
            downloads_dir=downloads_dir,
            timeout=args.timeout,
            retries=args.retries,
            use_doi=not args.skip_doi,
            max_candidates_per_item=args.max_candidates_per_item,
            workers=args.workers,
            show_progress=not args.no_progress,
        )
        if args.secondary_lookup:
            enrich_failed_references(
                refs,
                timeout=args.timeout,
                lookup_timeout=args.lookup_timeout,
                retries=args.retries,
                downloads_dir=downloads_dir,
                max_items=args.secondary_max,
                max_candidates_per_item=args.max_candidates_per_item,
                workers=args.workers,
                show_progress=not args.no_progress,
            )

    write_outputs(refs, output_dir)

    total = len(refs)
    ok_pdf = sum(1 for r in refs if r.download_status == "downloaded_pdf")
    ok_landing = sum(1 for r in refs if r.download_status == "saved_landing_url")
    failed = sum(1 for r in refs if r.download_status == "failed")
    print(f"Done. Parsed {total} references.")
    print(f"PDF downloaded: {ok_pdf}, landing URLs saved: {ok_landing}, failed: {failed}")
    print(f"Output directory: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
