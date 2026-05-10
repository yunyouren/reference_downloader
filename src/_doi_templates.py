"""DOI prefix to publisher PDF URL template mapping.

Each entry maps a DOI prefix to a URL template with placeholders:
- {doi}: URL-encoded full DOI
- {suffix}: URL-encoded last segment of DOI (after final /)
"""

from urllib.parse import quote

DOI_URL_TEMPLATES: list[tuple[str, str]] = [
    ("10.1007/", "https://link.springer.com/content/pdf/{doi}.pdf"),
    ("10.1088/", "https://iopscience.iop.org/article/{doi}/pdf"),
    ("10.1063/", "https://pubs.aip.org/aip/pdf/article/{doi}/pdf"),
    ("10.1103/", "https://journals.aps.org/prl/pdf/{doi}"),
    ("10.1098/", "https://royalsocietypublishing.org/doi/pdf/{doi}"),
    ("10.1017/", "https://www.cambridge.org/core/services/aop-cambridge-core/content/view/{doi}"),
    ("10.1038/", "https://www.nature.com/articles/{doi}.pdf"),
    ("10.1126/", "https://www.science.org/doi/pdf/{doi}"),
    ("10.1002/", "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"),
    ("10.1080/", "https://www.tandfonline.com/doi/pdf/{doi}"),
    ("10.1016/", "https://www.sciencedirect.com/science/article/pii/{suffix}/pdfft"),
    ("10.1146/", "https://www.annualreviews.org/doi/pdf/{doi}"),
    ("10.1021/", "https://pubs.acs.org/doi/pdf/{doi}"),
    ("10.1109/", "https://ieeexplore.ieee.org/document/{suffix}"),
    ("10.1145/", "https://dl.acm.org/doi/pdf/{doi}"),
    ("10.1093/", "https://academic.oup.com/article-pdf/{doi}"),
    ("10.1073/", "https://www.pnas.org/doi/pdf/{doi}"),
    ("10.1371/", "https://journals.plos.org/plosone/article/file?id={doi}&type=printable"),
    ("10.2307/", "https://www.jstor.org/stable/pdf/{suffix}.pdf"),
    ("10.3389/", "https://www.frontiersin.org/articles/{doi}/pdf"),
    ("10.3390/", "https://www.mdpi.com/{doi}/pdf"),
    ("10.1155/", "https://downloads.hindawi.com/journals/{doi}.pdf"),
    ("10.48550/", "https://arxiv.org/pdf/{suffix}.pdf"),
]


def build_doi_candidate(doi: str) -> str | None:
    """Return a direct PDF URL for a DOI based on its prefix, or None.

    If the DOI prefix matches a known publisher, returns the publisher-specific
    direct PDF URL. Returns None for unknown prefixes (caller should fall back
    to generic doi.org resolution).
    """
    d_clean = doi.strip()
    if not d_clean:
        return None
    d_lower = d_clean.lower()
    for prefix, template in DOI_URL_TEMPLATES:
        if d_lower.startswith(prefix):
            suffix = d_clean.split("/")[-1]  # preserve original case
            return template.format(doi=quote(d_clean, safe=""), suffix=quote(suffix, safe=""))
    return None
