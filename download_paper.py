#!/usr/bin/env python3
"""
Download a single paper by DOI, title, or URL — no reference list needed.

Usage:
    python download_paper.py --doi 10.1007/s11071-021-06487-3
    python download_paper.py --title "Neural ODE for power converter modeling"
    python download_paper.py --url https://arxiv.org/pdf/2301.00001.pdf
    python download_paper.py --doi 10.1007/xxx --cookies cookies.txt --output ./pdfs
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, quote

import requests

# Reuse existing infrastructure where available
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src._doi_templates import build_doi_candidate
from src.lookup import (
    lookup_unpaywall,
    lookup_arxiv_pdf_urls_by_title,
    lookup_semanticscholar_pdf_urls_by_title,
    lookup_openalex_pdf_urls_by_title,
    lookup_unpaywall_by_title,
    lookup_europepmc_pdf_urls_by_title,
    lookup_biorxiv_pdf_urls_by_title,
    unique_preserve_order,
)
from src.downloader import load_cookies_txt, make_session as make_download_session, merge_cookie_jars
from core.pdf_files import is_readable_pdf, save_pdf_stream
from core.verify import sanitize_filename_component
from core.verify import assess_pdf
from core.pdf_files import move_without_overwrite


def make_session(user_agent: str, cookies_path: Path | None = None) -> requests.Session:
    jar = load_cookies_txt(cookies_path) if cookies_path else None
    return make_download_session(pool_size=16, user_agent=user_agent,
                                 cookies_jar=merge_cookie_jars(jar, None))


def _try_fetch_pdf(
    session: requests.Session,
    url: str,
    timeout: int,
) -> tuple[bytes | None, str, str]:
    """Accept only structurally readable PDFs, never trust a suffix or MIME type."""
    resp = None
    try:
        resp = session.get(url, timeout=timeout, stream=True, allow_redirects=True)
        if not resp.ok:
            return None, "", ""
        ct = (resp.headers.get("content-type") or "").lower()
        final_url = resp.url or url

        content = resp.content
        if is_readable_pdf(content):
            return content, final_url, ct
        return None, "", ""
    except requests.RequestException:
        return None, "", ""
    finally:
        if resp is not None:
            resp.close()


def _resolve_filename(url: str, content_type: str | None = None) -> str:
    """Derive a clean filename from URL or Content-Disposition header."""
    path = urlparse(url).path
    name = Path(path).name
    if name and name.lower().endswith(".pdf"):
        return (sanitize_filename_component(name[:-4]) or "paper") + ".pdf"
    if content_type and "application/pdf" in content_type.lower():
        return "paper.pdf"
    return "paper.pdf"


def resolve_doi(session: requests.Session, doi: str, email: str, timeout: int) -> list[str]:
    """Resolve a DOI to candidate PDF URLs."""
    urls: list[str] = []

    # 1. Direct publisher template
    direct = build_doi_candidate(doi)
    if direct:
        urls.append(direct)

    # 2. Unpaywall OA
    oa = lookup_unpaywall(session, doi, email=email, timeout=timeout)
    if oa:
        urls.append(oa)

    # 3. Generic DOI resolution (doi.org)
    urls.append(f"https://doi.org/{quote(doi, safe='')}")

    return unique_preserve_order(urls)


def resolve_title(session: requests.Session, title: str, email: str, timeout: int) -> list[str]:
    """Resolve a paper title to candidate PDF URLs using multiple sources."""
    all_urls: list[str] = []

    sources = [
        ("arxiv", lambda: lookup_arxiv_pdf_urls_by_title(session, title, timeout=timeout)),
        ("semantic_scholar", lambda: lookup_semanticscholar_pdf_urls_by_title(session, title, timeout=timeout)),
        ("openalex", lambda: lookup_openalex_pdf_urls_by_title(session, title, timeout=timeout)),
        ("unpaywall", lambda: lookup_unpaywall_by_title(session, title, email=email, timeout=timeout)),
        ("europepmc", lambda: lookup_europepmc_pdf_urls_by_title(session, title, timeout=timeout)),
        ("biorxiv", lambda: lookup_biorxiv_pdf_urls_by_title(session, title, timeout=timeout)),
    ]

    for name, fn in sources:
        try:
            result = fn()
            all_urls.extend(result)
        except Exception:
            pass

    return unique_preserve_order(all_urls)


def download_paper(
    session: requests.Session,
    candidates: list[str],
    output_dir: Path,
    timeout: int,
    max_try: int = 5,
    *, expected_title: str = "", expected_doi: str = "",
) -> Path | None:
    """Try candidates sequentially; save the first valid PDF found."""
    tried = 0
    for url in candidates:
        tried += 1
        if max_try > 0 and tried > max_try:
            break

        print(f"  [{tried}] Trying: {url[:100]}...")
        content, final_url, content_type = _try_fetch_pdf(session, url, timeout)
        if content is not None:
            filename = _resolve_filename(final_url, content_type)
            if not filename.lower().endswith(".pdf"):
                filename = f"{filename}.pdf"
            output_path = save_pdf_stream(output_dir / filename, [content])
            if output_path is not None:
                decision = assess_pdf(out_file=output_path, expected_title=expected_title,
                    expected_dois=[expected_doi] if expected_doi else [], ref_year=None,
                    surname="", verify_title_threshold=.65, verify_weights=None,
                    check_identity=bool(expected_title or expected_doi), source_url=final_url)
                if decision.outcome == "downloaded_pdf":
                    print(f"  -> Saved: {output_path}")
                    return output_path
                folder = "review_pdfs" if decision.outcome == "needs_review" else "rejected_pdfs"
                retained = move_without_overwrite(output_path, output_dir / folder / output_path.name)
                print(f"  -> Not confirmed as main text ({decision.reason}): {retained}")

        # Brief pause between candidates
        if tried < min(max_try, len(candidates)):
            time.sleep(0.3)

    return None


def main():
    parser = argparse.ArgumentParser(description="Download a single paper by DOI, title, or URL")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--doi", help="Paper DOI (e.g. 10.1007/s11071-021-06487-3)")
    group.add_argument("--title", help="Paper title for search")
    group.add_argument("--url", help="Direct PDF or landing page URL")
    parser.add_argument("--output", "-o", default="downloaded_paper", help="Output directory (default: downloaded_paper/)")
    parser.add_argument("--cookies", help="Netscape cookies.txt for authenticated access")
    parser.add_argument("--unpaywall-email", default="", help="Email for Unpaywall API (recommended)")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds")
    parser.add_argument("--user-agent", default="PaperDownloader/1.0", help="HTTP User-Agent")
    parser.add_argument("--max-try", type=int, default=5, help="Max candidate URLs to try")
    args = parser.parse_args()

    cookies_path = Path(args.cookies) if args.cookies and Path(args.cookies).exists() else None
    session = make_session(args.user_agent, cookies_path)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates: list[str] = []

    if args.doi:
        print(f"Resolving DOI: {args.doi}")
        candidates = resolve_doi(session, args.doi, args.unpaywall_email, args.timeout)
        print(f"  {len(candidates)} candidate(s) found")

    elif args.title:
        print(f"Searching by title: {args.title}")
        candidates = resolve_title(session, args.title, args.unpaywall_email, args.timeout)
        print(f"  {len(candidates)} candidate(s) found from APIs")

    elif args.url:
        candidates = [args.url]

    if not candidates:
        print("No download candidates found.", file=sys.stderr)
        sys.exit(1)

    print("Downloading...")
    result = download_paper(session, candidates, output_dir, args.timeout, args.max_try,
                            expected_title=args.title or "", expected_doi=args.doi or "")

    if result:
        print(f"\nSuccess: {result}")
        sys.exit(0)
    else:
        print("\nFailed: could not download PDF from any candidate.", file=sys.stderr)
        print("Tips: try --unpaywall-email, --cookies, or use a direct --url.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
