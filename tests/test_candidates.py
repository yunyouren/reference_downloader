"""Tests for URL candidate generation and DOI template mapping."""
from src._doi_templates import build_doi_candidate, DOI_URL_TEMPLATES
from src.candidates import (
    normalize_generic_download_sites,
    build_generic_site_candidates,
)
from src.downloader import resolve_downloads_subdir
from src.models import ReferenceItem
from pathlib import Path


class TestBuildDoiCandidate:
    def test_springer(self):
        url = build_doi_candidate("10.1007/s11071-021-06487-3")
        assert url is not None
        assert "link.springer.com" in url
        assert "10.1007" in url

    def test_ieee(self):
        url = build_doi_candidate("10.1109/TPEL.2023.1234567")
        assert url is not None
        assert "ieeexplore.ieee.org" in url

    def test_arxiv(self):
        url = build_doi_candidate("10.48550/arXiv.2301.00001")
        assert url is not None
        assert "arxiv.org" in url

    def test_elsevier(self):
        url = build_doi_candidate("10.1016/j.egypro.2018.09.123")
        assert url is not None
        assert "sciencedirect.com" in url
        # Elsevier uses {suffix} not {doi}
        assert "pdfft" in url

    def test_nature(self):
        url = build_doi_candidate("10.1038/s41586-023-12345-6")
        assert url is not None
        assert "nature.com" in url

    def test_wiley(self):
        url = build_doi_candidate("10.1002/adma.202301234")
        assert url is not None
        assert "onlinelibrary.wiley.com" in url

    def test_acs(self):
        url = build_doi_candidate("10.1021/jacs.3c01234")
        assert url is not None
        assert "pubs.acs.org" in url

    def test_unknown_prefix(self):
        url = build_doi_candidate("10.99999/unknown.prefix")
        assert url is None

    def test_empty_doi(self):
        assert build_doi_candidate("") is None
        assert build_doi_candidate("  ") is None

    def test_case_preserving(self):
        # DOI is case-preserving — uppercase preserved in URL
        url_upper = build_doi_candidate("10.1007/S12345")
        url_lower = build_doi_candidate("10.1007/s12345")
        assert url_upper is not None
        assert url_lower is not None
        # Both resolve to Springer URL, prefix match is case-insensitive
        assert "link.springer.com" in url_upper
        assert "link.springer.com" in url_lower

    def test_mdpi(self):
        url = build_doi_candidate("10.3390/en16031234")
        assert url is not None
        assert "mdpi.com" in url


class TestDoiUrlTemplates:
    def test_all_templates_format(self):
        """Ensure all templates contain {doi} or {suffix} placeholder."""
        for prefix, template in DOI_URL_TEMPLATES:
            assert isinstance(prefix, str)
            assert isinstance(template, str)
            assert "{" in template, f"Template for {prefix} has no placeholder: {template}"
            assert any(
                p in template for p in ["{doi}", "{suffix}"]
            ), f"Template for {prefix} missing placeholder"

    def test_templates_are_unique(self):
        prefixes = [p for p, _ in DOI_URL_TEMPLATES]
        assert len(prefixes) == len(set(prefixes)), "Duplicate DOI prefixes found"

    def test_template_count(self):
        assert len(DOI_URL_TEMPLATES) >= 20, "Should have at least 20 publisher mappings"


class TestNormalizeGenericDownloadSites:
    def test_none_returns_empty(self):
        assert normalize_generic_download_sites(None) == []

    def test_string_with_commas(self):
        result = normalize_generic_download_sites(
            "https://sci-hub.se/{doi}, https://example.org/search?q={title_encoded}"
        )
        assert len(result) == 2
        assert result[0] == "https://sci-hub.se/{doi}"

    def test_list_input(self):
        result = normalize_generic_download_sites([
            "https://sci-hub.se/{doi}",
            "https://example.org/search?q={title_encoded}",
        ])
        assert len(result) == 2

    def test_filters_non_http(self):
        result = normalize_generic_download_sites(["ftp://files.com", "not-a-url"])
        assert result == []

    def test_deduplicates(self):
        result = normalize_generic_download_sites([
            "https://example.com/a",
            "https://example.com/a",
            "https://example.com/b",
        ])
        assert result == ["https://example.com/a", "https://example.com/b"]

    def test_filters_empty(self):
        result = normalize_generic_download_sites(["", "https://example.com", ""])
        assert result == ["https://example.com"]

    def test_non_string_non_list(self):
        assert normalize_generic_download_sites(12345) == []


class TestBuildGenericSiteCandidates:
    def test_expands_doi_placeholder(self):
        item = ReferenceItem(number=1, text="A sample paper", dois=["10.1000/abc"], urls=[])
        result = build_generic_site_candidates(item, ["https://sci-hub.se/{doi}"])
        assert len(result) == 1
        assert "10.1000/abc" in result[0]

    def test_expands_title_placeholder(self):
        item = ReferenceItem(number=1, text="Grid Stability Analysis", dois=[], urls=[])
        result = build_generic_site_candidates(
            item, ["https://example.org/search?q={title_encoded}"]
        )
        assert len(result) == 1
        assert "Grid" in result[0] or "grid" in result[0].lower()

    def test_no_sites_returns_empty(self):
        item = ReferenceItem(number=1, text="text", dois=["10.1000/abc"], urls=[])
        result = build_generic_site_candidates(item, None)
        assert result == []

    def test_deduplicates_results(self):
        item = ReferenceItem(number=1, text="text", dois=["10.1000/abc"], urls=[])
        result = build_generic_site_candidates(item, [
            "https://sci-hub.se/{doi}",
            "https://sci-hub.se/{doi}",
        ])
        assert len(result) == 1


class TestResolveDownloadsSubdir:
    def test_normal_subdir(self):
        result = resolve_downloads_subdir(Path("/tmp/dl"), "meta")
        assert result == Path("/tmp/dl/meta")

    def test_empty_subdir(self):
        assert resolve_downloads_subdir(Path("/tmp/dl"), "") is None
        assert resolve_downloads_subdir(Path("/tmp/dl"), "  ") is None
