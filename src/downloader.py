#!/usr/bin/env python3
"""Download pipeline for reference PDF retrieval.

Extracted from reference_tool.py — contains the session/config management,
download logic, verification, secondary lookup enrichment, and domain cookies
support.
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
from src.models import (
    ReferenceItem, DownloadAttempt, DownloadLogger,
    SecondaryLookupCache, DomainLimiter,
)
from src.candidates import iter_candidate_urls_with_generic_sites
from src.lookup import lookup_secondary_ranked, guess_title_query

try:
    from tqdm import tqdm  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore


# ---------------------------------------------------------------------------
# Session and config
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Download and verification
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Domain cookies and publisher
# ---------------------------------------------------------------------------

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
