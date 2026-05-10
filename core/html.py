from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse


def extract_citation_pdf_url(html_text: str, base_url: str = "") -> str | None:
    m = re.search(
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if m:
        raw = m.group(1).strip()
        return urljoin(base_url, raw) if base_url else raw
    return None


def extract_chinese_journal_pdf_url(html_text: str, base_url: str) -> str | None:
    """Extract PDF download URL from Chinese journal article pages.

    Covers Magtech/勤云 and similar platforms used by most Chinese engineering
    journals (中国电机工程学报, 电工技术学报, 电网技术, 电力系统自动化, etc.).
    """
    # 1) citation_pdf_url meta tag (many journals support this now)
    meta_pdf = extract_citation_pdf_url(html_text, base_url)
    if meta_pdf:
        return meta_pdf

    # 2) downloadArticleFile.do — classic Magtech platform pattern
    m = re.search(
        r"""href=["']([^"']*downloadArticleFile\.do[^"']*attachType=PDF[^"']*)["']""",
        html_text,
    )
    if m:
        return urljoin(base_url, m.group(1).strip())

    # 3) Links with PDF text — "PDF", "下载PDF", "全文PDF", "在线阅读"
    for pattern in [
        r"""<a[^>]+href=["']([^"']*\.pdf[^"']*)["'][^>]*>[^<]*(?:PDF|下载|全文)[^<]*</a>""",
        r"""<a[^>]+href=["']([^"']*\.pdf[^"']*)["']""",
    ]:
        m = re.search(pattern, html_text, flags=re.IGNORECASE)
        if m:
            return urljoin(base_url, m.group(1).strip())

    # 4) <iframe> or <embed> pointing to PDF
    for tag in ("iframe", "embed"):
        m = re.search(
            rf"""<{tag}[^>]+src=["']([^"']*\.pdf[^"']*)["']""",
            html_text,
            flags=re.IGNORECASE,
        )
        if m:
            return urljoin(base_url, m.group(1).strip())

    # 5) Magtech article page — extract article ID and build PDF URL
    m = re.search(r"/article/(?:showArticle|show)\.do\?id=(\d+)", base_url)
    if not m:
        m = re.search(r"articleId[=:](\d+)", html_text)
    if m:
        article_id = m.group(1)
        return urljoin(base_url, f"downloadArticleFile.do?attachType=PDF&id={article_id}")

    return None


def extract_springer_pdf_url(html_text: str, base_url: str) -> str | None:
    meta_pdf = extract_citation_pdf_url(html_text, base_url)
    if meta_pdf:
        return meta_pdf
    p = urlparse(base_url)
    if (p.hostname or "").lower() != "link.springer.com":
        return None
    m2 = re.search(r"^/(article|chapter)/([^/?#]+)", p.path, flags=re.IGNORECASE)
    if not m2:
        return None
    doi = m2.group(2)
    return f"https://link.springer.com/content/pdf/{doi}.pdf"


def extract_ieee_arnumber(url: str) -> str | None:
    try:
        p = urlparse(url)
        if (p.hostname or "").lower() != "ieeexplore.ieee.org":
            return None
        m = re.search(r"^/document/(\d+)/?", p.path)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def extract_ieee_pdf_url(html_text: str, base_url: str, arnumber: str) -> str | None:
    iframe_match = re.search(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", html_text, flags=re.IGNORECASE)
    if iframe_match:
        iframe_src = iframe_match.group(1).strip()
        if "stampPDF/getPDF.jsp" in iframe_src and f"arnumber={arnumber}" in iframe_src:
            return urljoin(base_url, iframe_src)
    m0 = re.search(
        r"(https?://ieeexplore\.ieee\.org/stampPDF/getPDF\.jsp[^\"'\s>]*arnumber="
        + re.escape(arnumber)
        + r"[^\"'\s>]*)",
        html_text,
        flags=re.IGNORECASE,
    )
    if m0:
        return m0.group(1)
    m0b = re.search(
        r"(/stampPDF/getPDF\.jsp[^\"'\s>]*arnumber=" + re.escape(arnumber) + r"[^\"'\s>]*)",
        html_text,
        flags=re.IGNORECASE,
    )
    if m0b:
        return urljoin(base_url, m0b.group(1))
    m = re.search(r"https?://ieeexplore\.ieee\.org/ielx[^\"]+?arnumber=" + re.escape(arnumber), html_text, flags=re.IGNORECASE)
    if m:
        return m.group(0)
    m2 = re.search(r"(/ielx[^\"]+?arnumber=" + re.escape(arnumber) + r")", html_text, flags=re.IGNORECASE)
    if m2:
        return urljoin(base_url, m2.group(1))
    return None
