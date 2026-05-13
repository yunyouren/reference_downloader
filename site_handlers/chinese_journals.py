"""HTML handler for Chinese academic journal platforms.

Covers the Magtech/勤云 platform used by most Chinese engineering journals
(中国电机工程学报, 电工技术学报, 电网技术, 电力系统自动化, 高电压技术, etc.)
as well as custom journal sites.
"""

from __future__ import annotations

import json
import random as _random
import re
import time as _time
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests

from .registry import HandlerResult, register
from core import collect_stream_text

try:
    from pdfplumber import open as pdfplumber_open
except ImportError:
    pdfplumber_open = None


# Domains of major Chinese electrical engineering journals
_CHINESE_JOURNAL_HOSTS = [
    # Magtech-based
    "www.pcsee.org",            # 中国电机工程学报
    "pcsee.org",
    "www.ces-transaction.com",  # 电工技术学报
    "ces-transaction.com",
    "www.aeps-info.com",         # 电力系统自动化
    "aeps-info.com",
    "www.dwjs.com.cn",           # 电网技术
    "dwjs.com.cn",
    "hve.epri.sgcc.com.cn",      # 高电压技术
    "www.jops.cn",               # 电源学报
    "jops.cn",
    # Additional common Chinese journal domains
    "www.jee-cet.com",           # 电气工程学报
    "www.jspe.sgcc.com.cn",      # 电力工程技术
    # Generic CNKI journal article pages
    "kns.cnki.net",
    "navi.cnki.net",
]


@register(_CHINESE_JOURNAL_HOSTS)
def handle_chinese_journal_html(
    *,
    session: requests.Session,
    item,
    helpers,
    downloads_dir,
    mismatch_dir,
    verified_dir,
    timeout: int,
    attempt: int,
    verify_title_rename: bool,
    verify_title_threshold: float,
    verify_rename_mode: str,
    verify_weights,
    logger,
    phase: str,
    seen: set[str],
    prefix: str,
    final_url: str,
    first_chunk: bytes,
    chunks: Iterable[bytes],
) -> HandlerResult:
    parse_retry_after_seconds = helpers["parse_retry_after_seconds"]
    is_probably_pdf = helpers["is_probably_pdf"]
    verify_downloaded_pdf_and_update_item = helpers["verify_downloaded_pdf_and_update_item"]
    extract_chinese_journal_pdf_url = helpers["extract_chinese_journal_pdf_url"]
    DownloadAttempt = helpers["DownloadAttempt"]

    html_text = collect_stream_text(first_chunk, chunks)
    pdf_url = extract_chinese_journal_pdf_url(html_text, base_url=final_url)
    if not pdf_url or pdf_url in seen:
        return "unhandled"
    seen.add(pdf_url)

    pdf_response: requests.Response | None = None
    try:
        pdf_response = session.get(
            pdf_url,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
            headers={
                "Referer": final_url,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        if pdf_response.status_code in (408, 425, 429, 500, 502, 503, 504):
            retry_after = parse_retry_after_seconds(
                pdf_response.headers.get("retry-after") or ""
            )
            waited_s = (
                retry_after
                if retry_after is not None
                else min(30.0, (2.0 ** attempt) + _random.random() * 0.25)
            )
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
            _time.sleep(waited_s)
            return "retry"
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
            return "continue"

        final_pdf_url = pdf_response.url or pdf_url
        pdf_chunks = pdf_response.iter_content(chunk_size=1024 * 64)
        pdf_first_chunk = b""
        for chunk in pdf_chunks:
            if chunk:
                pdf_first_chunk = chunk
                break
        if not (pdf_first_chunk and is_probably_pdf(pdf_first_chunk)):
            return "continue"

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
                handled = verify_downloaded_pdf_and_update_item(
                    item=item,
                    out_file=out_file,
                    downloads_dir=downloads_dir,
                    verified_dir=verified_dir,
                    mismatch_dir=mismatch_dir,
                    final_url=final_pdf_url,
                    candidate_url=pdf_url,
                    status_code=int(pdf_response.status_code),
                    content_type=(pdf_response.headers.get("content-type") or ""),
                    phase=phase,
                    logger=logger,
                    verify_title_threshold=float(verify_title_threshold),
                    verify_rename_mode=str(verify_rename_mode or "number_and_original"),
                    verify_weights=verify_weights,
                )
                return "downloaded" if item.download_status == "downloaded_pdf" else "continue"

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
            return "downloaded"
        finally:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
    finally:
        if pdf_response is not None:
            pdf_response.close()
