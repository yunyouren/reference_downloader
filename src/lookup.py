"""Secondary lookup functionality extracted from reference_tool.py.

Includes helper utilities for title guessing, year/author parsing, title scoring,
and a suite of lookup functions that query various academic APIs (Crossref, OpenAlex,
Unpaywall, arXiv, bioRxiv, Europe PMC, Semantic Scholar, CORE, Google Books, SSRN,
ChemRxiv, ResearchGate, NeurIPS proceedings) to find PDF URLs and DOIs for references.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Iterable
from urllib.parse import quote, urljoin

import requests  # type: ignore[import-untyped]

from src.models import ReferenceItem, SecondaryLookupCandidate, DomainLimiter


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def guess_title_query(ref_text: str) -> str:
    """
    从参考文献条目中猜测一个"更像标题"的查询字符串，用于二次检索。

    做法：
    - 去掉引号等符号；
    - 按常见分隔符切成片段（.;。；），取最长片段作为候选；
    - 尝试剔除 vol/no/pp 等尾部信息，避免检索噪声；
    - 最终限制长度，避免请求参数过长。
    """
    m = re.findall(r"[\"""]([^\"""]{8,300})[\"""]", ref_text)
    if m:
        best = max((s.strip() for s in m), key=len, default="")
        if best:
            return best[:180].strip()

    tmp = re.sub(r"['\"""‘’]", "", ref_text)
    parts = [p.strip() for p in re.split(r"[.;。；]", tmp) if p.strip()]
    if not parts:
        return ref_text[:120]
    best = max(parts, key=len)
    best = re.sub(r"\b(?:vol|no|pp|ed|dept|univ|university)\b.*$", "", best, flags=re.IGNORECASE)
    return best[:180].strip()


def parse_ref_year(ref_text: str) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", ref_text)
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def parse_first_author_surname(ref_text: str) -> str:
    txt = (ref_text or "").strip()
    if not txt:
        return ""
    m = re.match(r"^\s*([A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\\-]{1,40})\b", txt)
    if m:
        return m.group(1).lower()
    m2 = re.search(r"\b([A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'`\\-]{1,40})\s*,", txt)
    if m2:
        return m2.group(1).lower()
    return ""


def secondary_title_score(candidate_title: str, expected_title: str) -> float:
    def tokens(text: str) -> set[str]:
        raw = (text or "").lower()
        raw = re.sub(r"[‐-―−]", "-", raw)
        raw = re.sub(r"[^a-z0-9]+", " ", raw)
        toks = [t for t in raw.split() if len(t) >= 3]
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
        return {t for t in toks if t not in stop}

    a = tokens(candidate_title)
    b = tokens(expected_title)
    if not a or not b:
        return 0.0
    return float(len(a & b)) / float(len(a | b))


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def lookup_secondary_ranked(
    session: requests.Session,
    item: ReferenceItem,
    timeout: int,
    top_k: int,
    api_limiter: DomainLimiter | None = None,
) -> tuple[list[str], list[str]]:
    expected = guess_title_query(item.text)
    ref_year = parse_ref_year(item.text)
    surname = parse_first_author_surname(item.text)
    author_query = surname if len(surname) >= 3 else ""
    candidates: list[SecondaryLookupCandidate] = []
    min_keep_score = 0.12

    def url_priority(url: str) -> int:
        u = (url or "").lower()
        if "stampdf/getpdf.jsp" in u or u.endswith(".pdf") or "/content/pdf/" in u:
            return 0
        if "/ielx" in u and "arnumber=" in u:
            return 1
        if "doi.org/" in u:
            return 3
        return 2

    try:
        crossref_params: dict[str, str | int] = {"query.bibliographic": expected, "rows": 5}
        if author_query:
            crossref_params["query.author"] = author_query
        if ref_year:
            crossref_params["filter"] = f"from-pub-date:{ref_year}-01-01,until-pub-date:{ref_year}-12-31"
        sem = api_limiter.acquire("api.crossref.org") if api_limiter is not None else None
        try:
            res = session.get(
                "https://api.crossref.org/works",
                params=crossref_params,
                timeout=timeout,
            )
        finally:
            if sem is not None:
                sem.release()
        if res.ok:
            items = res.json().get("message", {}).get("items", [])
            for it in items:
                title = ""
                raw_title = it.get("title") or []
                if isinstance(raw_title, list) and raw_title:
                    title = str(raw_title[0] or "")
                doi = (it.get("DOI") or "").strip()
                urls: list[str] = []
                item_url = (it.get("URL") or "").strip()
                if item_url:
                    urls.append(item_url)
                for link in it.get("link", []) or []:
                    link_url = link.get("URL")
                    if link_url:
                        urls.append(str(link_url))
                urls = sorted(unique_preserve_order(urls), key=url_priority)
                if doi or urls:
                    base = secondary_title_score(title, expected)
                    if not title.strip() or base < min_keep_score:
                        continue
                    bonus = 0.0
                    try:
                        year_parts = (it.get("issued", {}) or {}).get("date-parts", []) or []
                        y = int(year_parts[0][0]) if year_parts and year_parts[0] else None
                        if ref_year and y and abs(y - ref_year) <= 0:
                            bonus += 0.08
                    except Exception:
                        pass
                    try:
                        auth = it.get("author") or []
                        if author_query and isinstance(auth, list) and auth:
                            family = str((auth[0] or {}).get("family") or "").lower()
                            if family and family == author_query:
                                bonus += 0.06
                    except Exception:
                        pass
                    candidates.append(
                        SecondaryLookupCandidate(
                            score=base + bonus,
                            doi=doi,
                            urls=urls,
                        )
                    )
    except Exception:
        pass

    try:
        openalex_params: dict[str, str | int] = {"search": expected, "per-page": 5}
        if ref_year:
            openalex_params["filter"] = f"publication_year:{ref_year}"
        sem = api_limiter.acquire("api.openalex.org") if api_limiter is not None else None
        try:
            res = session.get(
                "https://api.openalex.org/works",
                params=openalex_params,
                timeout=timeout,
            )
        finally:
            if sem is not None:
                sem.release()
        if res.ok:
            results = res.json().get("results", [])
            for row in results:
                title = str((row.get("title") or "")).strip()
                doi_url = str((row.get("doi") or "")).strip()
                doi = ""
                urls: list[str] = []
                if doi_url:
                    urls.append(doi_url)
                    if doi_url.lower().startswith("https://doi.org/"):
                        doi = doi_url.split("doi.org/", 1)[1]
                open_access = row.get("open_access", {}) or {}
                oa_url = str((open_access.get("oa_url") or "")).strip()
                if oa_url:
                    urls.append(oa_url)
                primary_location = row.get("primary_location", {}) or {}
                landing = str((primary_location.get("landing_page_url") or "")).strip()
                if landing:
                    urls.append(landing)
                urls = sorted(unique_preserve_order(urls), key=url_priority)
                if doi or urls:
                    base = secondary_title_score(title, expected)
                    if not title.strip() or base < min_keep_score:
                        continue
                    bonus = 0.0
                    try:
                        y = int(row.get("publication_year") or 0) or None
                        if ref_year and y and abs(y - ref_year) <= 0:
                            bonus += 0.08
                    except Exception:
                        pass
                    try:
                        auths = row.get("authorships") or []
                        if author_query and isinstance(auths, list) and auths:
                            name = (((auths[0] or {}).get("author") or {}).get("display_name") or "")
                            if name and author_query in name.lower().split():
                                bonus += 0.06
                    except Exception:
                        pass
                    candidates.append(
                        SecondaryLookupCandidate(
                            score=base + bonus,
                            doi=doi,
                            urls=urls,
                        )
                    )
    except Exception:
        pass

    candidates.sort(key=lambda c: (c.score, 1 if c.doi else 0, len(c.urls)), reverse=True)
    selected = candidates if top_k <= 0 else candidates[: max(1, top_k)]

    dois: list[str] = []
    urls: list[str] = []
    for c in selected:
        if c.doi:
            dois.append(c.doi)
        urls.extend(c.urls)
    return unique_preserve_order(dois), unique_preserve_order(urls)


# ---------------------------------------------------------------------------
# Individual lookup functions
# ---------------------------------------------------------------------------

def lookup_crossref_by_bibliographic(
    session: requests.Session,
    item: ReferenceItem,
    timeout: int,
) -> tuple[list[str], list[str]]:
    """
    使用 Crossref API 按"书目信息"检索（query.bibliographic），尽量补全 DOI/URL。

    注意：
    - 这是二次检索（secondary lookup），只对初次下载失败项使用更合理；
    - 返回值为 (dois, urls)，已去重排序；
    - 网络错误/非 2xx 会直接返回空列表，不中断整体流程。
    """
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
    """
    使用 OpenAlex Works 搜索接口尝试补全 DOI/开放获取链接。

    OpenAlex 返回的 doi 字段常见格式为 "https://doi.org/..."，这里会同时：
    - 保存该 URL；
    - 若是 doi.org 链接，则提取出纯 DOI 字符串，加入 found_dois。
    """
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


def lookup_unpaywall(
    session: requests.Session,
    doi: str,
    email: str = "test@example.com",
    timeout: int = 10,
) -> str | None:
    """
    使用 Unpaywall API 查找开放获取PDF链接。

    Unpaywall 是一个免费的开放获取数据库，包含合法的免费PDF链接。
    无需登录/cookies，覆盖大量期刊。

    Args:
        session: requests.Session
        doi: DOI字符串
        email: 邮箱（Unpaywall要求提供，但可以是假邮箱）
        timeout: 超时秒数

    Returns:
        开放获取PDF的URL，如果没有则返回None
    """
    if not doi:
        return None
    try:
        url = f"https://api.unpaywall.org/v2/{quote(doi, safe='')}?email={email}"
        res = session.get(url, timeout=timeout)
        if not res.ok:
            return None
        data = res.json()

        # 检查是否是开放获取
        if data.get("is_oa"):
            # 获取最佳OA位置
            best_oa = data.get("best_oa_location") or {}
            oa_url = best_oa.get("url_for_pdf") or best_oa.get("url")
            if oa_url:
                return oa_url

        # 检查所有OA位置
        for loc in data.get("oa_locations", []) or []:
            oa_url = loc.get("url_for_pdf") or loc.get("url")
            if oa_url:
                return oa_url

    except requests.RequestException:
        pass
    except Exception:
        pass
    return None


def is_neurips_reference(ref_text: str) -> bool:
    s = (ref_text or "").lower()
    if "neural inf. process. syst" in s:
        return True
    if "neural information processing systems" in s:
        return True
    return bool(re.search(r"\b(neurips|nips)\b", s))


def lookup_neurips_proceedings_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    ref_year: int | None,
    timeout: int,
    max_results: int = 5,
) -> list[str]:
    title = re.sub(r"\s+", " ", (expected_title or "").strip().strip(" \t\r\n,.;:，。；："))
    if not title:
        return []
    try:
        res = session.get(
            "https://proceedings.neurips.cc/papers/search",
            params={"q": title},
            timeout=timeout,
        )
        if not res.ok:
            return []
        html = res.text
        links = re.findall(r'href=["\']([^"\']+Abstract[^"\']*\.html)["\']', html, flags=re.IGNORECASE)
        if not links:
            return []
        abs_links = unique_preserve_order([urljoin("https://proceedings.neurips.cc", u) for u in links])
        abs_links = abs_links[: max(1, min(3, int(max_results)))]
        out: list[tuple[int, float, str]] = []
        for page_url in abs_links:
            try:
                page = session.get(page_url, timeout=timeout)
                if not page.ok:
                    continue
                time.sleep(0.25)
                page_html = page.text
                m_title = re.search(
                    r'<meta[^>]+name=["\']citation_title["\'][^>]+content=["\']([^"\']+)["\']',
                    page_html,
                    flags=re.IGNORECASE,
                )
                page_title = re.sub(r"\s+", " ", (m_title.group(1) if m_title else "").strip())
                if not page_title:
                    continue
                score = secondary_title_score(page_title, title)
                if score < 0.6:
                    continue
                m_pdf = re.search(
                    r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+\.pdf)["\']',
                    page_html,
                    flags=re.IGNORECASE,
                )
                pdf_url = (m_pdf.group(1) if m_pdf else "").strip()
                if not pdf_url:
                    m_pdf2 = re.search(r'href=["\']([^"\']+-Paper\.pdf)["\']', page_html, flags=re.IGNORECASE)
                    if m_pdf2:
                        pdf_url = urljoin(page_url, m_pdf2.group(1).strip())
                if not pdf_url:
                    continue
                year_bonus = 0
                m_year = re.search(r"/paper_files/paper/(\d{4})/", page_url)
                if ref_year and m_year:
                    try:
                        y = int(m_year.group(1))
                        if y == int(ref_year):
                            year_bonus = 1
                    except Exception:
                        pass
                out.append((year_bonus, float(score), pdf_url))
            except requests.RequestException:
                continue
            except Exception:
                continue
        out.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return unique_preserve_order([u for _, _, u in out][: max(1, int(max_results))])
    except requests.RequestException:
        return []
    except Exception:
        return []



def lookup_arxiv_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "http://export.arxiv.org/api/query",
            params={
                "search_query": f'ti:\"{title}\"',
                "start": 0,
                "max_results": int(max(1, min(25, max_results))),
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        root = ET.fromstring(res.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out: list[tuple[float, str]] = []
        for entry in root.findall("a:entry", ns):
            entry_title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
            entry_title = re.sub(r"\s+", " ", entry_title)
            score = secondary_title_score(entry_title, title)
            if score < 0.6:
                continue
            entry_id = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
            if not entry_id:
                continue
            arxiv_id = entry_id.rsplit("/", 1)[-1]
            arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
            if not arxiv_id:
                continue
            out.append((score, f"https://arxiv.org/pdf/{arxiv_id}.pdf"))
        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_biorxiv_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 bioRxiv/medRxiv 预印本 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # 使用 bioRxiv API 搜索
        res = session.get(
            "https://api.biorxiv.org/details/biorxiv",
            params={
                "title": title[:200],  # 限制标题长度
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        if not data.get("messages") or data["messages"][0].get("status") != "ok":
            return []

        out: list[tuple[float, str]] = []
        for item in data.get("collection", [])[:5]:
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue
            doi = item.get("doi", "")
            if doi:
                # bioRxiv PDF URL 格式
                out.append((score, f"https://www.biorxiv.org/content/{quote(doi, safe='')}.full.pdf"))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_europepmc_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 Europe PMC 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={
                "query": f'TITLE:"{title}"',
                "format": "json",
                "pageSize": 5,
                "openAccess": "true",  # 只返回开放获取
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("resultList", {}).get("result", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 检查是否有开放获取 PDF
            pmcid = item.get("pmcid", "")
            if pmcid:
                out.append((score, f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"))

            # 也检查 DOI
            doi = item.get("doi", "")
            if doi and item.get("isOpenAccess") == "Y":
                # 尝试获取 fullText URL
                full_text_url = item.get("fullTextUrlList", {}).get("fullTextUrl", [])
                for url_info in full_text_url:
                    if url_info.get("documentStyle") == "pdf":
                        out.append((score, url_info.get("url", "")))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []

def lookup_core_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 CORE 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # CORE API 搜索
        res = session.get(
            "https://api.core.ac.uk/v3/search/works",
            params={
                "q": f'title:"{title}"',
                "limit": 5,
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("results", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 获取下载URL
            download_url = item.get("downloadUrl", "")
            if download_url and ".pdf" in download_url.lower():
                out.append((score, download_url))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_google_books_pdf_urls(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 Google Books。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={
                "q": f'intitle:"{title}"',
                "maxResults": 5,
                "filter": "free-ebooks",  # 只返回免费电子书
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("items", []):
            volume_info = item.get("volumeInfo", {})
            item_title = (volume_info.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 检查是否有 PDF 下载链接
            access_info = item.get("accessInfo", {})
            pdf_link = access_info.get("pdf", {}).get("downloadLink", "")
            if pdf_link:
                out.append((score, pdf_link))

            # 也检查 webReaderLink
            web_reader = access_info.get("webReaderLink", "")
            if web_reader and access_info.get("viewability") == "ALL_PAGES":
                out.append((score, web_reader))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_crossref_tdm_urls(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过 Crossref TDM (Text and Data Mining) API 查找 PDF 链接。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://api.crossref.org/works",
            params={
                "query.title": title,
                "rows": 5,
                "select": "title,link,DOI",
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("message", {}).get("items", []):
            titles = item.get("title", [])
            item_title = titles[0] if titles else ""
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 检查 link 字段
            for link in item.get("link", []):
                content_type = link.get("content-type", "")
                url = link.get("URL", "")
                if url and "pdf" in content_type.lower():
                    out.append((score, url))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_ssrn_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 SSRN 预印本 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # SSRN 搜索 API
        res = session.get(
            "https://api.ssrn.com/content/v1/papers/search",
            params={
                "query": f'title:"{title}"',
                "limit": 5,
            },
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("papers", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            ssrn_id = item.get("ssrn_id") or item.get("paperId")
            if ssrn_id:
                # SSRN PDF URL 格式
                out.append((score, f"https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID/{ssrn_id}.pdf"))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_chemrxiv_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 ChemRxiv 预印本 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # ChemRxiv 使用 Figshare API
        res = session.get(
            "https://api.figshare.com/v2/articles/search",
            params={
                "search": f'title:"{title}"',
                "resource_type": "publication",
                "institution": 513,  # ChemRxiv institution ID
                "page_size": 5,
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data:
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 获取 PDF 链接
            for file_info in item.get("files", []):
                if file_info.get("is_link_only") is False:
                    pdf_url = file_info.get("download_url", "")
                    if pdf_url:
                        out.append((score, pdf_url))
                        break

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_researchgate_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 ResearchGate 开放获取 PDF。

    注意：ResearchGate 没有官方 API，此函数尝试解析搜索页面。
    成功率可能较低，但作为补充来源。
    """
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # ResearchGate 搜索页面
        res = session.get(
            "https://www.researchgate.net/search",
            params={
                "q": title[:200],
                "type": "publication",
            },
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
            },
        )
        if not res.ok:
            return []

        html = res.text
        out: list[tuple[float, str]] = []

        # 尝试从页面提取 PDF 链接
        # 匹配 publication 页面中的 PDF 链接
        pdf_patterns = [
            r'href="(/publication/\d+_[^"]+\.pdf)"',
            r'href="(/profile/[^/]+/publication/\d+_[^"]+\.pdf)"',
            r'"pdfUrl"\s*:\s*"([^"]+)"',
        ]

        for pattern in pdf_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches[:3]:
                if match.startswith("/"):
                    match = f"https://www.researchgate.net{match}"
                out.append((0.6, match))

        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_unpaywall_by_title(
    session: requests.Session,
    title: str,
    email: str = "test@example.com",
    timeout: int = 10,
) -> list[str]:
    """通过标题搜索 Unpaywall 开放获取 PDF。

    先通过 Crossref 查找 DOI，再通过 Unpaywall 查找 PDF。
    """
    title = (title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # 先通过 Crossref 查找 DOI
        res = session.get(
            "https://api.crossref.org/works",
            params={
                "query.title": title,
                "rows": 3,
            },
            timeout=timeout,
        )
        if not res.ok:
            return []

        data = res.json()
        out: list[str] = []

        for item in data.get("message", {}).get("items", []):
            doi = item.get("DOI", "")
            if not doi:
                continue

            # 检查标题匹配度
            item_title = (item.get("title", [""])[0] if isinstance(item.get("title"), list) else item.get("title", "") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 通过 Unpaywall 查找 PDF
            oa_url = lookup_unpaywall(session, doi, email=email, timeout=timeout)
            if oa_url:
                out.append(oa_url)

        return unique_preserve_order(out)
    except Exception:
        return []


def lookup_openalex_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 OpenAlex 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        # OpenAlex 使用 search 参数进行标题搜索
        res = session.get(
            "https://api.openalex.org/works",
            params={
                "search": title,  # 直接使用标题搜索
                "per-page": 5,
                "mailto": "api@example.com",  # Polite pool
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("results", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            # 检查开放获取
            oa_status = item.get("open_access", {})
            if oa_status.get("is_oa"):
                oa_url = oa_status.get("oa_url", "")
                if oa_url:
                    out.append((score, oa_url))

            # 也检查 locations
            for location in item.get("locations", []):
                pdf_url = location.get("pdf_url", "")
                if pdf_url:
                    out.append((score, pdf_url))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []


def lookup_semanticscholar_pdf_urls_by_title(
    session: requests.Session,
    expected_title: str,
    timeout: int,
) -> list[str]:
    """通过标题搜索 Semantic Scholar 开放获取 PDF。"""
    title = (expected_title or "").strip().strip(" \t\r\n,.;:，。；：")
    if not title:
        return []
    try:
        res = session.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": title,
                "limit": 5,
                "fields": "title,openAccessPdf",
            },
            timeout=timeout,
        )
        if not res.ok:
            return []
        data = res.json()
        out: list[tuple[float, str]] = []

        for item in data.get("data", []):
            item_title = (item.get("title") or "").strip()
            score = secondary_title_score(item_title, title)
            if score < 0.5:
                continue

            oa_pdf = item.get("openAccessPdf", {})
            if oa_pdf and oa_pdf.get("url"):
                out.append((score, oa_pdf["url"]))

        out.sort(key=lambda x: x[0], reverse=True)
        return unique_preserve_order([u for _, u in out])
    except Exception:
        return []
