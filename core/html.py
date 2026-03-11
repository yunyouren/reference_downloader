from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse


def extract_springer_pdf_url(html_text: str, base_url: str) -> str | None:
    m = re.search(
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if m:
        return urljoin(base_url, m.group(1).strip())
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
