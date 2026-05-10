"""URL candidate generation and publisher HTML handlers."""

from __future__ import annotations

import math
import random
import re
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse

import requests  # type: ignore[import-untyped]

import site_handlers
from core.http import is_probably_pdf, parse_retry_after_seconds, should_record_landing_url
from core.html import extract_springer_pdf_url, extract_ieee_arnumber, extract_ieee_pdf_url
from core.urls import normalize_candidate_url
from core.verify import (
    VerifyWeights, build_verified_pdf_name, extract_pdf_best_line_score,
    extract_pdf_first_page_text, extract_pdf_title_from_file,
    move_verified_pdf, title_match_score, unique_path, verify_and_rename_pdf,
)
from src._doi_templates import build_doi_candidate
from src.models import ReferenceItem, DownloadAttempt, DomainLimiter, DownloadLogger
from src.lookup import guess_title_query, parse_ref_year, parse_first_author_surname

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
            candidate = build_doi_candidate(d)
            if candidate is not None:
                yield candidate
            # Always fall back to generic DOI resolution
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

