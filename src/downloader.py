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
import os
from copy import copy, deepcopy
from itertools import chain
from http.cookiejar import Cookie, MozillaCookieJar
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests  # type: ignore[import-untyped]
from requests.adapters import HTTPAdapter  # type: ignore[import-untyped]

import site_handlers
from core import collect_stream_text
from core.http import is_probably_pdf, parse_retry_after_seconds, should_record_landing_url
from core.pdf_files import contained_file, file_sha256, is_readable_pdf, save_pdf_stream
from core.document import VERIFICATION_VERSION
from core.runtime import RunControl, RunCancelled, ControlledSession, LimitedAdapter
from core.html import extract_springer_pdf_url, extract_chinese_journal_pdf_url, extract_ieee_arnumber, extract_ieee_pdf_url
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
    assess_pdf,
)
from src.models import (
    ReferenceItem, DownloadAttempt, DownloadLogger,
    SecondaryLookupCache, DomainLimiter, PipelineConfig,
)
from src.candidates import iter_candidate_urls_with_generic_sites, normalize_generic_download_sites
from src.lookup import (
    lookup_secondary_ranked, guess_title_query, parse_ref_year,
    parse_first_author_surname, unique_preserve_order, is_neurips_reference,
    lookup_neurips_proceedings_pdf_urls_by_title, lookup_arxiv_pdf_urls_by_title,
    lookup_semanticscholar_pdf_urls_by_title, lookup_europepmc_pdf_urls_by_title,
    lookup_biorxiv_pdf_urls_by_title, lookup_core_pdf_urls_by_title,
    lookup_openalex_pdf_urls_by_title, lookup_crossref_tdm_urls,
    lookup_google_books_pdf_urls, lookup_ssrn_pdf_urls_by_title,
    lookup_chemrxiv_pdf_urls_by_title, lookup_researchgate_pdf_urls_by_title,
    lookup_unpaywall, lookup_unpaywall_by_title,
)
from src.parsers import read_pdf_text, extract_references_section, split_references, DOI_RE
from src.output import write_outputs

_DEBUG = os.environ.get("REFERENCE_DEBUG") or os.environ.get("DEBUG")

try:
    from tqdm import tqdm  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore


# ---------------------------------------------------------------------------
# Session and config
# ---------------------------------------------------------------------------

def _same_reference(item: ReferenceItem, row: dict) -> bool:
    def doi_set(values) -> set[str]:
        if not isinstance(values, list):
            return set()
        return {re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value).strip(), flags=re.I).casefold().rstrip(".,;")
                for value in values if str(value).strip()}

    current_dois, old_dois = doi_set(item.dois), doi_set(row.get("dois"))
    if current_dois and old_dois:
        return bool(current_dois & old_dois)
    # Legacy manifests with no identity must be retried, not matched by number.
    old_text = row.get("text")
    if not isinstance(old_text, str) or not old_text.strip():
        return False
    normalize = lambda text: re.sub(r"\s+", " ", text).strip().casefold()
    return bool(item.text.strip()) and normalize(item.text) == normalize(old_text)


def apply_resume_state(refs: list[ReferenceItem], output_dir: Path, downloads_dir: Path) -> None:
    state_file = output_dir / "references.json"
    if not state_file.exists():
        return
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        if _DEBUG:
            print(f"[DEBUG downloader] Failed to parse resume state: {state_file}", file=sys.stderr)
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
        if item is None or not _same_reference(item, row):
            continue
        status = str(row.get("download_status") or "")
        downloaded_file = str(row.get("downloaded_file") or "")
        note = str(row.get("note") or "")
        if status not in {"downloaded_pdf", "saved_landing_url", "needs_review"}:
            continue
        p = contained_file(downloads_dir, downloaded_file)
        if p is None:
            continue
        if status == "saved_landing_url":
            # Preserve a useful candidate, but a landing page is not completion.
            try:
                url = p.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                continue
            if urlparse(url).scheme in {"http", "https"}:
                item.urls = unique_preserve_order(item.urls + [url])
            continue
        if not is_readable_pdf(p):
            continue
        expected_hash = row.get("file_sha256")
        if expected_hash:
            try:
                if file_sha256(p) != expected_hash:
                    continue
            except OSError:
                continue
        item.download_status = status
        item.downloaded_file = p.relative_to(downloads_dir.resolve()).as_posix()
        item.note = note
        item.document_kind = str(row.get("document_kind") or "")
        item.verification_reason = str(row.get("verification_reason") or "")
        item.identity_checked = row.get("identity_checked") is True
        try:
            item.verification_version = int(row.get("verification_version") or 0)
        except (ValueError, TypeError):
            item.verification_version = 0


def merge_cookie_jars(
    cookies_jar: MozillaCookieJar | None,
    domain_cookies: dict[str, MozillaCookieJar] | None,
) -> MozillaCookieJar | None:
    """Use one domain-scoped jar per session, including DOI redirects and fallbacks.

    Requests selects applicable cookies for each URL, including redirects. Never
    turn cookies into a global Cookie header. Configured jars override duplicates.
    """
    if cookies_jar is None and not domain_cookies:
        return None
    merged = MozillaCookieJar()
    jars = ([cookies_jar] if cookies_jar is not None else []) + list((domain_cookies or {}).values())
    for jar in jars:
        for cookie in jar:
            # A domainless cookie would be sent to unrelated hosts by requests.
            if cookie.domain:
                merged.set_cookie(copy(cookie))
    return merged


def make_session(pool_size: int, user_agent: str, cookies_jar: MozillaCookieJar | None,
                 *, control: RunControl | None = None, domain_limiter=None, api_limiter=None) -> requests.Session:
    """
    创建带连接池的 requests.Session，用于多线程下载时复用连接。

    - pool_size: HTTPAdapter 的连接池大小（连接数上限）
    - user_agent: 便于对端识别的 UA（避免一些站点拦截默认 UA）
    """
    session = ControlledSession(control) if control is not None else requests.Session()
    adapter_kwargs = dict(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    adapter = LimitedAdapter(control=control, domain_limiter=domain_limiter,
        api_limiter=api_limiter, **adapter_kwargs) if control is not None else HTTPAdapter(**adapter_kwargs)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": user_agent})
    if cookies_jar is not None:
        for cookie in cookies_jar:
            session.cookies.set_cookie(copy(cookie))
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
            if _DEBUG:
                print(f"[DEBUG downloader] JSON cookie parsing failed", file=sys.stderr)
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
    check_identity: bool = True,
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
        expected_dois=DOI_RE.findall(item.text),
        expected_identifiers=re.findall(r"\b(?:AT|US|EP|WO|CN)\s?\d[\d /-]{4,18}\s?[ABC]\d?\b", item.text, flags=re.I),
        source_url=final_url,
        check_identity=check_identity,
        expected_kind="presentation" if re.search(r"keynote|tutorial|presentation|lecture|slides|演讲|课件", item.text, flags=re.I) else "",
    )
    item.document_kind = decision.document_kind
    item.verification_reason = decision.reason
    item.verification_version = VERIFICATION_VERSION
    item.identity_checked = check_identity
    if decision.outcome == "downloaded_pdf":
        item.download_status = "downloaded_pdf"
        item.downloaded_file = decision.rel_path
        item.note = (
            f"{final_url} | identity={decision.identity_match} | kind={decision.document_kind} | title_match={decision.score:.3f} | title_score={decision.title_score:.3f} | line_score={decision.line_score:.3f} | year_hit={int(decision.year_hit)} | author_hit={int(decision.author_hit)}"
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
            outcome=decision.outcome,
            waited_seconds=0.0,
            error=f"reason={decision.reason}; kind={decision.document_kind}; file={decision.rel_path}; score={decision.score:.3f}; title_score={decision.title_score:.3f}; line_score={decision.line_score:.3f}; year_hit={int(decision.year_hit)}; author_hit={int(decision.author_hit)}; best_line={decision.best_line[:120]}; pdf_title={decision.pdf_title[:120]}",
        )
    )
    if decision.outcome == "needs_review":
        item.download_status = "needs_review"
        item.downloaded_file = decision.rel_path
        item.note = f"{final_url} | review={decision.reason}"
    else:
        item.download_status = "failed"
        item.downloaded_file = ""
        item.note = f"{final_url} | rejected={decision.reason}"
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
    control: RunControl | None = None,
) -> None:
    """
    尝试为单条参考文献下载 PDF，或保存落地页 URL。

    输出策略：
    - 总是写入 00X_meta.txt（条目原文，便于人工追溯）；
    - PDF 必须通过文件头和结构校验，保存时保留原编号且不覆盖旧文件；
    - 否则保存最终跳转的落地页 URL 到 00X_landing.url.txt。
    """
    prefix = f"{item.number:03d}"
    control = control or RunControl()
    control.check()
    if item.download_status == "downloaded_pdf" and item.downloaded_file:
        existing = contained_file(downloads_dir, item.downloaded_file)
        if existing is not None and is_readable_pdf(existing):
            if item.verification_version == VERIFICATION_VERSION and (not verify_title_rename or item.identity_checked):
                return
            decision = assess_pdf(out_file=existing, expected_title=guess_title_query(item.text),
                ref_year=parse_ref_year(item.text), surname=parse_first_author_surname(item.text),
                verify_title_threshold=verify_title_threshold, verify_weights=verify_weights,
                expected_dois=DOI_RE.findall(item.text), check_identity=verify_title_rename,
                expected_identifiers=re.findall(r"\b(?:AT|US|EP|WO|CN)\s?\d[\d /-]{4,18}\s?[ABC]\d?\b", item.text, flags=re.I),
                expected_kind="presentation" if re.search(r"keynote|tutorial|presentation|lecture|slides|演讲|课件", item.text, flags=re.I) else "")
            item.verification_version = VERIFICATION_VERSION
            item.identity_checked = verify_title_rename
            item.document_kind, item.verification_reason = decision.document_kind, decision.reason
            if decision.outcome == "downloaded_pdf":
                return
            if decision.outcome == "needs_review":
                item.download_status = "needs_review"
                item.note = f"Existing PDF needs review: {decision.reason}"
    best_review = deepcopy(item) if item.download_status == "needs_review" else None
    item.download_status = "not_attempted"
    item.downloaded_file = ""
    item.note = ""
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
        control.check()
        if candidate in seen:
            continue
        seen.add(candidate)
        tried += 1
        if max_candidates_per_item > 0 and tried > max_candidates_per_item:
            break

        for attempt in range(max(1, retries)):
            control.check()
            try:
                host = urlparse(candidate).hostname or ""
                sem = None if isinstance(session, ControlledSession) else domain_limiter.acquire(host, cancel_event=control.event)
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
                        control.wait(waited_s)
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
                        out_file = save_pdf_stream(downloads_dir / f"{prefix}.pdf", chain([first_chunk], chunks))
                        if out_file is None:
                            logger.add(DownloadAttempt(
                                phase=phase, ref_number=item.number, candidate_url=candidate,
                                final_url=final_url, status_code=int(response.status_code),
                                content_type=response.headers.get("content-type") or "",
                                outcome="invalid_pdf", waited_seconds=0.0,
                                error="PDF could not be parsed or contains no readable pages",
                            ))
                            break
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
                            verify_rename_mode=str(verify_rename_mode) if verify_title_rename else "number_only",
                            verify_weights=verify_weights,
                            check_identity=verify_title_rename,
                        )
                        if item.download_status == "downloaded_pdf":
                            return
                        if item.download_status == "needs_review":
                            best_review = deepcopy(item)
                        break

                    content_type = (response.headers.get("content-type") or "")
                    best_landing_content_type = content_type
                    if "text/html" in content_type.lower():
                        host = (urlparse(final_url).hostname or "").lower()
                        html_payload = collect_stream_text(first_chunk, chunks).encode("utf-8")
                        response.close()
                        response = None
                        def verify_site_pdf(**kwargs):
                            return verify_downloaded_pdf_and_update_item(**kwargs, check_identity=verify_title_rename)
                        helpers = {
                            "parse_retry_after_seconds": parse_retry_after_seconds,
                            "is_probably_pdf": is_probably_pdf,
                            "verify_downloaded_pdf_and_update_item": verify_site_pdf,
                            "wait": control.wait,
                            "extract_springer_pdf_url": extract_springer_pdf_url,
                            "extract_chinese_journal_pdf_url": extract_chinese_journal_pdf_url,
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
                            verify_title_rename=True,
                            verify_title_threshold=float(verify_title_threshold),
                            verify_rename_mode=str(verify_rename_mode) if verify_title_rename else "number_only",
                            verify_weights=verify_weights,
                            logger=logger,
                            phase=phase,
                            seen=seen,
                            prefix=prefix,
                            final_url=final_url,
                            first_chunk=html_payload,
                            chunks=iter(()),
                        )
                        if item.download_status == "needs_review":
                            best_review = deepcopy(item)
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
                control.wait(waited_s)
                continue

    if best_review is not None:
        item.__dict__.update(best_review.__dict__)
        return
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
    control: RunControl | None = None,
    on_item_complete=None,
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
    control = control or RunControl()

    thread_local = threading.local()
    domain_limiter = DomainLimiter(max_per_domain, min_delay_ms=min_domain_delay_ms)
    session_cookies = merge_cookie_jars(cookies_jar, domain_cookies)
    sessions = []
    session_lock = threading.Lock()

    def get_session() -> requests.Session:
        """One thread-local session; cookies remain scoped to each request host."""
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session(pool_size=max(8, workers * 2), user_agent=user_agent,
                cookies_jar=session_cookies, control=control, domain_limiter=domain_limiter)
            with session_lock:
                sessions.append(thread_local.session)
        return thread_local.session

    def worker(item: ReferenceItem) -> None:
        control.check()
        session = get_session()
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
            control=control,
        )

    try:
        _run_workers(refs, worker, workers, logger, "initial", control, on_item_complete)
    finally:
        _close_sessions(sessions)


def _close_sessions(sessions):
    for session in sessions:
        close = getattr(session, "close", None)
        if callable(close):
            close()


def _run_workers(items, worker, workers, logger, phase, control, on_item_complete=None):
    """Bound the work queue so cancellation prevents submission of later items."""
    source = iter(items)
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        pending = {}
        def submit_next():
            if control.event.is_set():
                return
            item = next(source, None)
            if item is not None:
                original = deepcopy(item)
                pending[executor.submit(worker, item)] = (item, original)
        for _ in range(max(1, workers)):
            submit_next()
        while pending:
            ready, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            for future in ready:
                item, original = pending.pop(future)
                try:
                    _collect_worker_result(future, item, logger, phase)
                except RunCancelled:
                    if item.download_status != "downloaded_pdf":
                        item.__dict__.update(original.__dict__)
                        item.download_status = "needs_review" if original.downloaded_file and original.download_status in {"needs_review", "downloaded_pdf"} else "not_attempted"
                        item.note = "Stopped; retained prior files, retry this item on the next run."
                completed += 1
                if on_item_complete is not None:
                    on_item_complete(item)
                reason = f" | {item.note[:180]}" if item.download_status in {"failed", "needs_review"} else ""
                control.emit(f"{phase}: [{item.number}] {item.download_status} ({completed}/{len(items)}){reason}")
                submit_next()


def _collect_worker_result(future, item: ReferenceItem, logger: DownloadLogger, phase: str) -> None:
    try:
        future.result()
    except RunCancelled:
        raise
    except Exception as exc:
        item.download_status = "failed"
        item.downloaded_file = ""
        item.note = f"{phase} worker error: {type(exc).__name__}: {exc}"
        logger.add(DownloadAttempt(
            phase=phase, ref_number=item.number, candidate_url="", final_url="",
            status_code=0, content_type="", outcome="worker_exception",
            waited_seconds=0.0, error=item.note,
        ))
        print(f"Reference [{item.number}] {item.note}", file=sys.stderr)


def enrich_failed_references(
    refs: list[ReferenceItem], timeout: int, lookup_timeout: int, retries: int,
    downloads_dir: Path, meta_dir: Path | None, landing_dir: Path | None,
    mismatch_dir: Path | None, max_items: int, max_candidates_per_item: int,
    secondary_top_k: int, workers: int, show_progress: bool, user_agent: str,
    max_per_domain: int, min_domain_delay_ms: int, logger: DownloadLogger,
    cookies_jar: MozillaCookieJar | None, verify_title_rename: bool,
    verify_title_threshold: float, verify_rename_mode: str, verify_weights,
    verified_dir: Path | None, secondary_cache: SecondaryLookupCache | None,
    unpaywall_email: str = "", generic_download_sites: list[str] | None = None,
    api_concurrency: int = 1, api_min_delay_ms: int = 500,
    neurips_proceedings: bool = True, domain_cookies: dict[str, MozillaCookieJar] | None = None,
    control: RunControl | None = None, on_item_complete=None, time_budget: float = 90,
) -> None:
    """Resolve and download in stages; successful main text short-circuits sources."""
    failed = [r for r in refs if r.download_status in {"failed", "saved_landing_url", "needs_review"}]
    if max_items > 0:
        failed = failed[:max_items]
    if not failed:
        return
    control = control or RunControl()
    domain_limiter = DomainLimiter(max_per_domain, min_delay_ms=min_domain_delay_ms)
    api_limiter = DomainLimiter(max(1, api_concurrency), min_delay_ms=max(0, api_min_delay_ms))
    session_cookies = merge_cookie_jars(cookies_jar, domain_cookies)
    thread_local = threading.local()
    sessions = []
    session_lock = threading.Lock()

    def worker(item):
        control.check()
        if not hasattr(thread_local, "session"):
            thread_local.session = make_session(pool_size=max(8, workers * 2), user_agent=user_agent,
                cookies_jar=session_cookies, control=control, domain_limiter=domain_limiter, api_limiter=api_limiter)
            with session_lock:
                sessions.append(thread_local.session)
        session = thread_local.session
        expected = guess_title_query(item.text)
        deadline = time.monotonic() + max(0.0, time_budget) if time_budget > 0 else float("inf")
        attempted = set()
        best_review = deepcopy(item) if item.download_status == "needs_review" else None
        errors = []

        def download_candidates():
            nonlocal best_review
            control.check()
            urls = [u for u in iter_candidate_urls_with_generic_sites(item, True, generic_download_sites) if u not in attempted]
            urls = unique_preserve_order(urls)
            if max_candidates_per_item > 0:
                urls = urls[:max_candidates_per_item]
            if not urls:
                return False
            attempted.update(urls)
            trial = deepcopy(item)
            trial.urls, trial.dois = urls, []
            try_download(session=session, item=trial, downloads_dir=downloads_dir,
                meta_dir=meta_dir, landing_dir=landing_dir, mismatch_dir=mismatch_dir,
                timeout=timeout, retries=retries, use_doi=False,
                max_candidates_per_item=max_candidates_per_item, domain_limiter=domain_limiter,
                logger=logger, phase="secondary", verify_title_rename=verify_title_rename,
                verify_title_threshold=verify_title_threshold, verify_rename_mode=verify_rename_mode,
                verify_weights=verify_weights, verified_dir=verified_dir, control=control)
            if trial.download_status == "needs_review":
                best_review = trial
            if trial.download_status == "downloaded_pdf":
                item.download_status, item.downloaded_file, item.note = trial.download_status, trial.downloaded_file, trial.note
                item.document_kind, item.verification_reason, item.verification_version = trial.document_kind, trial.verification_reason, trial.verification_version
                item.identity_checked = trial.identity_checked
                item.note += " | resolved_by=secondary_lookup"
                return True
            if trial.download_status == "saved_landing_url" and item.download_status != "needs_review":
                item.download_status, item.downloaded_file, item.note = trial.download_status, trial.downloaded_file, trial.note
            return False

        def ranked():
            return lookup_secondary_ranked(session, item=item, timeout=lookup_timeout,
                top_k=secondary_top_k, api_limiter=None if isinstance(session, ControlledSession) else api_limiter)

        sources = [("metadata", ranked)]
        if neurips_proceedings and is_neurips_reference(item.text):
            sources.insert(0, ("neurips", lambda: ([], lookup_neurips_proceedings_pdf_urls_by_title(
                session, expected, parse_ref_year(item.text), timeout=lookup_timeout))))
        if unpaywall_email:
            sources.append(("unpaywall", lambda: ([], unique_preserve_order(
                [u for d in item.dois if (u := lookup_unpaywall(session, d, email=unpaywall_email, timeout=lookup_timeout))]
                if item.dois else lookup_unpaywall_by_title(session, expected, email=unpaywall_email, timeout=lookup_timeout)))))
        for label, fn in [("openalex", lookup_openalex_pdf_urls_by_title),
                          ("semanticscholar", lookup_semanticscholar_pdf_urls_by_title),
                          ("arxiv", lookup_arxiv_pdf_urls_by_title),
                          ("core", lookup_core_pdf_urls_by_title),
                          ("crossref_tdm", lookup_crossref_tdm_urls)]:
            sources.append((label, lambda fn=fn: ([], fn(session, expected, timeout=lookup_timeout))))
        text = item.text.lower()
        if re.search(r"biolog|biomed|clinical|biorxiv|medrxiv|pubmed|medicine", text):
            for label, fn in [("europepmc", lookup_europepmc_pdf_urls_by_title), ("biorxiv", lookup_biorxiv_pdf_urls_by_title)]:
                sources.append((label, lambda fn=fn: ([], fn(session, expected, timeout=lookup_timeout))))
        if re.search(r"chemrxiv|chemistry|chemical", text):
            sources.append(("chemrxiv", lambda: ([], lookup_chemrxiv_pdf_urls_by_title(session, expected, timeout=lookup_timeout))))
        if re.search(r"ssrn|economic|finance|business", text):
            sources.append(("ssrn", lambda: ([], lookup_ssrn_pdf_urls_by_title(session, expected, timeout=lookup_timeout))))
        if re.search(r"isbn|textbook|\bbook\b|\bedition\b", text):
            sources.append(("googlebooks", lambda: ([], lookup_google_books_pdf_urls(session, expected, timeout=lookup_timeout))))
        sources.append(("researchgate", lambda: ([], lookup_researchgate_pdf_urls_by_title(session, expected, timeout=lookup_timeout))))

        for label, lookup in sources:
            control.check()
            if time.monotonic() >= deadline:
                errors.append("lookup time budget reached")
                break
            key = hashlib.sha256(json.dumps({"v":4,"source":label,"title":expected,
                "text":item.text,"k":secondary_top_k,"email":unpaywall_email,
                "doi":sorted(item.dois) if label=="unpaywall" else []}, sort_keys=True).encode()).hexdigest()
            cached = secondary_cache.get(key) if secondary_cache is not None else None
            before = getattr(session, "transient_errors", 0)
            try:
                result = cached if cached is not None else lookup()
                control.check()
                dois, urls = result
                after = getattr(session, "transient_errors", 0)
                transient = isinstance(before, int) and isinstance(after, int) and after > before
                if secondary_cache is not None and cached is None and not transient:
                    secondary_cache.set(key, dois, urls)
                item.dois = unique_preserve_order(list(dois) + item.dois)
                item.urls = unique_preserve_order(list(urls) + item.urls)
                logger.add(DownloadAttempt("secondary", item.number, label, "", 0, "",
                    "cache_hit" if cached is not None else "lookup_found" if dois or urls else "lookup_transient" if transient else "lookup_miss", 0.0, ""))
            except RunCancelled:
                raise
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                logger.add(DownloadAttempt("secondary", item.number, label, "", 0, "", "lookup_exception", 0.0, errors[-1]))
                continue
            if download_candidates():
                return
        if best_review is not None:
            # Keep all discovered candidates, not just the trial's subset.
            item.download_status, item.downloaded_file, item.note = best_review.download_status, best_review.downloaded_file, best_review.note
            item.document_kind, item.verification_reason, item.verification_version = best_review.document_kind, best_review.verification_reason, best_review.verification_version
            item.identity_checked = best_review.identity_checked
        if errors:
            item.note = (item.note + " | " + "; ".join(errors)).strip(" |")

    try:
        _run_workers(failed, worker, workers, logger, "secondary", control, on_item_complete)
    finally:
        _close_sessions(sessions)
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
        if _DEBUG:
            print(f"[DEBUG downloader] Failed to load domain cookies config: {path}", file=sys.stderr)
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
                if _DEBUG:
                    print(f"[DEBUG downloader] URL parsing failed in suggest_cookies_configuration", file=sys.stderr)
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
                if _DEBUG:
                    print(f"[DEBUG downloader] Failed to load cookie jar for {domain}: {path}", file=sys.stderr)
                pass
    return result


def run_pipeline_from_refs(
    refs: list[ReferenceItem],
    cfg: PipelineConfig,
    *,
    verify_weights: VerifyWeights | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> list[ReferenceItem]:
    """Run download pipeline from pre-parsed reference items.

    Args:
        refs: Pre-parsed reference items (from PDF, docx, or any source).
        cfg: PipelineConfig with all settings.
        verify_weights: Optional custom VerifyWeights override.

    Returns the list of reference items with download status populated.
    """
    from site_handlers.domain_analyzer import analyze_reference_domains
    from src.interactive_ui import should_run_interactive, display_domain_summary, configure_cookies_interactively
    control = RunControl(cancel_event, progress_callback)
    control.check()

    if verify_weights is None:
        verify_weights = VerifyWeights()

    # Load cookies
    cookies_jar: MozillaCookieJar | None = None
    if cfg.cookies_path and cfg.cookies_path.exists():
        cookies_jar = load_cookies_txt(cfg.cookies_path)

    # Resolve directories
    downloads_dir = cfg.output_dir / "downloads"
    meta_dir = resolve_downloads_subdir(downloads_dir, cfg.meta_subdir)
    landing_dir = resolve_downloads_subdir(downloads_dir, cfg.landing_subdir)
    mismatch_dir = resolve_downloads_subdir(downloads_dir, cfg.mismatch_subdir)
    verified_dir: Path | None = None
    if cfg.verify_title_rename:
        verified_dir = resolve_downloads_subdir(downloads_dir, cfg.verified_subdir)

    # Domain analysis and interactive configuration
    domain_cookies_file_path = Path(cfg.domain_cookies_file)
    if not domain_cookies_file_path.is_absolute():
        domain_cookies_file_path = cfg.output_dir / domain_cookies_file_path
    domain_cookies_config = cfg.domain_cookies_config
    if domain_cookies_config is None:
        domain_cookies_config = load_domain_cookies_config(domain_cookies_file_path)

    domain_info = analyze_reference_domains(refs, domain_cookies_config)
    if progress_callback is None:
        display_domain_summary(domain_info)
    else:
        control.emit(f"Parsed {len(refs)} references; {len(domain_info)} source domains.")

    is_interactive = should_run_interactive(cfg.interactive)
    if is_interactive:
        new_config = configure_cookies_interactively(domain_info, domain_cookies_config)
        if new_config != domain_cookies_config:
            domain_cookies_config = new_config
            save_domain_cookies_config(domain_cookies_config, domain_cookies_file_path)
            print(f"\n已保存域名cookies配置到: {domain_cookies_file_path}")

    domain_cookies = load_domain_cookies(domain_cookies_config, Path.cwd())
    sites = normalize_generic_download_sites(cfg.generic_download_sites or [])

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    if cfg.resume:
        apply_resume_state(refs, output_dir=cfg.output_dir, downloads_dir=downloads_dir)

    snapshots = deepcopy(refs)
    positions = {id(item): i for i, item in enumerate(refs)}
    hash_cache = {}
    def checkpoint(item):
        snapshots[positions[id(item)]] = deepcopy(item)
        write_outputs(snapshots, cfg.output_dir, hash_cache=hash_cache)
        if cfg.download_log:
            logger.write_csv(cfg.output_dir / cfg.download_log)

    # Download phases
    if not cfg.no_download:
        logger = DownloadLogger()
        write_outputs(snapshots, cfg.output_dir, hash_cache=hash_cache)
        secondary_cache: SecondaryLookupCache | None = None
        cache_str = cfg.secondary_cache.strip()
        if cache_str:
            cache_path = Path(cache_str)
            if not cache_path.is_absolute():
                cache_path = cfg.output_dir / cache_path
            secondary_cache = SecondaryLookupCache(cache_path)
        initial_refs = refs[:cfg.download_max] if cfg.download_max > 0 else refs
        run_initial_download_phase(
            initial_refs,
            downloads_dir=downloads_dir,
            meta_dir=meta_dir,
            landing_dir=landing_dir,
            mismatch_dir=mismatch_dir,
            timeout=cfg.timeout,
            retries=cfg.retries,
            use_doi=not cfg.skip_doi,
            max_candidates_per_item=cfg.max_candidates_per_item,
            workers=cfg.workers,
            show_progress=cfg.show_progress,
            user_agent=cfg.user_agent,
            max_per_domain=cfg.max_per_domain,
            min_domain_delay_ms=cfg.min_domain_delay_ms,
            logger=logger,
            cookies_jar=cookies_jar,
            verify_title_rename=cfg.verify_title_rename,
            verify_title_threshold=cfg.verify_title_threshold,
            verify_rename_mode=cfg.verify_rename_mode,
            verify_weights=verify_weights,
            verified_dir=verified_dir,
            domain_cookies=domain_cookies,
            generic_download_sites=sites,
            control=control,
            on_item_complete=checkpoint,
        )
        if cfg.secondary_lookup and not control.event.is_set():
            enrich_failed_references(
                initial_refs,
                timeout=cfg.timeout,
                lookup_timeout=cfg.lookup_timeout,
                retries=cfg.retries,
                downloads_dir=downloads_dir,
                meta_dir=meta_dir,
                landing_dir=landing_dir,
                mismatch_dir=mismatch_dir,
                max_items=cfg.secondary_max,
                max_candidates_per_item=cfg.max_candidates_per_item,
                secondary_top_k=cfg.secondary_top_k,
                workers=cfg.workers,
                show_progress=cfg.show_progress,
                user_agent=cfg.user_agent,
                max_per_domain=cfg.max_per_domain,
                min_domain_delay_ms=cfg.min_domain_delay_ms,
                logger=logger,
                cookies_jar=cookies_jar,
                domain_cookies=domain_cookies,
                verify_title_rename=cfg.verify_title_rename,
                verify_title_threshold=cfg.verify_title_threshold,
                verify_rename_mode=cfg.verify_rename_mode,
                verify_weights=verify_weights,
                verified_dir=verified_dir,
                secondary_cache=secondary_cache,
                unpaywall_email=cfg.unpaywall_email,
                generic_download_sites=sites,
                api_concurrency=cfg.api_concurrency,
                api_min_delay_ms=cfg.api_min_delay_ms,
                neurips_proceedings=cfg.neurips_proceedings,
                control=control,
                on_item_complete=checkpoint,
            )
        if cfg.download_log:
            logger.write_csv(cfg.output_dir / cfg.download_log)

    # Write outputs
    write_outputs(refs, cfg.output_dir)

    # Summary
    total = len(refs)
    ok_pdf = sum(1 for r in refs if r.download_status == "downloaded_pdf")
    ok_landing = sum(1 for r in refs if r.download_status == "saved_landing_url")
    failed = sum(1 for r in refs if r.download_status == "failed")
    review = sum(1 for r in refs if r.download_status == "needs_review")
    control.emit(f"{'Stopped' if control.event.is_set() else 'Done'}. Parsed {total} references.")
    control.emit(f"PDF downloaded: {ok_pdf}, review: {review}, landing URLs saved: {ok_landing}, failed: {failed}")
    control.emit(f"Output directory: {cfg.output_dir.resolve()}")

    if failed > 0:
        suggest_cookies_configuration(refs, domain_cookies_config, cfg.output_dir)

    return refs


def run_pipeline(
    cfg: PipelineConfig,
    *,
    verify_weights: VerifyWeights | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> list[ReferenceItem]:
    """Run the full reference extraction and download pipeline from a PDF.

    Args:
        cfg: PipelineConfig with all settings.
        verify_weights: Optional custom VerifyWeights override.

    Returns the list of reference items with download status populated.
    """
    # Validate PDF input
    control = RunControl(cancel_event, progress_callback)
    control.check()
    if not cfg.input_pdf.exists():
        raise FileNotFoundError(f"Input PDF does not exist: {cfg.input_pdf}")
    if not cfg.input_pdf.is_file():
        raise ValueError(f"Input PDF is not a file: {cfg.input_pdf}")
    if cfg.input_pdf.stat().st_size <= 0:
        raise ValueError(f"Input PDF is empty: {cfg.input_pdf}")

    # Read PDF text
    try:
        full_text = read_pdf_text(
            cfg.input_pdf,
            parser=cfg.pdf_parser,
            header_margin=cfg.header_margin,
            footer_margin=cfg.footer_margin,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to read input PDF: {cfg.input_pdf} ({exc})") from exc

    # Extract and parse references
    ref_section = extract_references_section(full_text)
    refs = split_references(ref_section)
    control.check()
    return run_pipeline_from_refs(refs, cfg, verify_weights=verify_weights,
        cancel_event=control.event, progress_callback=progress_callback)
