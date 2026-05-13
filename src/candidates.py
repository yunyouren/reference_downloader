"""URL candidate generation for reference download tool."""

from __future__ import annotations

from typing import Iterable
from urllib.parse import quote

from core.urls import normalize_candidate_url
from src._doi_templates import build_doi_candidate
from src.models import ReferenceItem
from src.lookup import guess_title_query


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

    for url in sorted(item.urls, key=candidate_priority):
        normalized = normalize_candidate_url(url)
        if normalized:
            yield normalized
    if use_doi:
        for doi in item.dois:
            d = str(doi or "").strip()
            if not d:
                continue
            candidate = build_doi_candidate(d)
            if candidate is not None:
                yield candidate
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
