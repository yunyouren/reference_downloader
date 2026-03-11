from __future__ import annotations

from typing import Iterable

import requests

from .registry import HandlerResult, register


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


@register(["link.springer.com"])
def handle_springer_html(
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
    verify_weights,
    logger,
    phase: str,
    seen: set[str],
    prefix: str,
    final_url: str,
    first_chunk: bytes,
    chunks: Iterable[bytes],
) -> HandlerResult:
    import random as _random
    import time as _time
    parse_retry_after_seconds = helpers["parse_retry_after_seconds"]
    is_probably_pdf = helpers["is_probably_pdf"]
    verify_downloaded_pdf_and_update_item = helpers["verify_downloaded_pdf_and_update_item"]
    extract_springer_pdf_url = helpers["extract_springer_pdf_url"]
    DownloadAttempt = helpers["DownloadAttempt"]

    html_text = collect_stream_text(first_chunk, chunks)
    pdf_url = extract_springer_pdf_url(html_text, base_url=final_url)
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
        )
        if pdf_response.status_code in (408, 425, 429, 500, 502, 503, 504):
            retry_after = parse_retry_after_seconds(pdf_response.headers.get("retry-after") or "")
            waited_s = retry_after if retry_after is not None else min(30.0, (2.0**attempt) + _random.random() * 0.25)
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
