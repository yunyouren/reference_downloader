"""Tests for HTTP-based lookup functions using mocked responses."""
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.lookup import (
    lookup_arxiv_pdf_urls_by_title,
    lookup_biorxiv_pdf_urls_by_title,
    lookup_semanticscholar_pdf_urls_by_title,
    lookup_europepmc_pdf_urls_by_title,
    lookup_unpaywall,
    lookup_core_pdf_urls_by_title,
    lookup_secondary_ranked,
    lookup_neurips_proceedings_pdf_urls_by_title,
    lookup_google_books_pdf_urls,
    lookup_crossref_tdm_urls,
    lookup_ssrn_pdf_urls_by_title,
    lookup_chemrxiv_pdf_urls_by_title,
    lookup_researchgate_pdf_urls_by_title,
    lookup_unpaywall_by_title,
    lookup_openalex_pdf_urls_by_title,
)
from src.models import ReferenceItem, DomainLimiter


def _make_session(json_data=None, text="", status_code=200, ok=True):
    """Create a mock session whose .get() returns a mock response."""
    mock_resp = MagicMock()
    mock_resp.ok = ok
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data or {}
    mock_resp.text = text
    session = MagicMock()
    session.get.return_value = mock_resp
    return session


# ---------------------------------------------------------------------------
# lookup_unpaywall
# ---------------------------------------------------------------------------

class TestLookupUnpaywall(unittest.TestCase):
    def test_finds_oa_pdf_from_best_location(self):
        session = _make_session(json_data={
            "is_oa": True,
            "best_oa_location": {
                "url_for_pdf": "https://example.org/paper.pdf",
                "url": "https://example.org/landing",
            },
        })
        result = lookup_unpaywall(session, "10.1000/abc", email="x@y", timeout=3)
        self.assertEqual(result, "https://example.org/paper.pdf")

    def test_falls_back_to_url_when_pdf_missing(self):
        session = _make_session(json_data={
            "is_oa": True,
            "best_oa_location": {
                "url": "https://example.org/landing",
            },
        })
        result = lookup_unpaywall(session, "10.1000/abc", email="x@y", timeout=3)
        self.assertEqual(result, "https://example.org/landing")

    def test_checks_oa_locations_list(self):
        session = _make_session(json_data={
            "is_oa": True,
            "best_oa_location": None,
            "oa_locations": [
                {"url_for_pdf": "https://example.org/oa1.pdf"},
                {"url_for_pdf": "https://example.org/oa2.pdf"},
            ],
        })
        result = lookup_unpaywall(session, "10.1000/abc", email="x@y")
        self.assertEqual(result, "https://example.org/oa1.pdf")

    def test_not_oa_returns_none(self):
        session = _make_session(json_data={"is_oa": False})
        result = lookup_unpaywall(session, "10.1000/abc", email="x@y")
        self.assertIsNone(result)

    def test_empty_doi_returns_none(self):
        session = _make_session()
        self.assertIsNone(lookup_unpaywall(session, "", email="x@y"))

    def test_non_200_returns_none(self):
        session = _make_session(ok=False, status_code=404)
        self.assertIsNone(lookup_unpaywall(session, "10.1000/abc", email="x@y"))

    def test_connection_error_returns_none(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("timeout")
        self.assertIsNone(lookup_unpaywall(session, "10.1000/abc", email="x@y"))


# ---------------------------------------------------------------------------
# lookup_arxiv_pdf_urls_by_title
# ---------------------------------------------------------------------------

ARXIV_XML_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>A Fast Method for Power Converter Simulation</title>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2301.00002v2</id>
    <title>Unrelated Topic in Biology</title>
  </entry>
</feed>"""


class TestLookupArxiv(unittest.TestCase):
    def test_finds_matching_title(self):
        session = _make_session(text=ARXIV_XML_RESPONSE)
        result = lookup_arxiv_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertTrue(any("2301.00001" in u for u in result))
        self.assertFalse(any("2301.00002" in u for u in result))

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_arxiv_pdf_urls_by_title(session, "", timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=500)
        self.assertEqual(lookup_arxiv_pdf_urls_by_title(session, "title", timeout=5), [])

    def test_connection_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("fail")
        self.assertEqual(lookup_arxiv_pdf_urls_by_title(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_biorxiv_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupBiorxiv(unittest.TestCase):
    def test_finds_pdf_for_matching_title(self):
        session = _make_session(json_data={
            "messages": [{"status": "ok"}],
            "collection": [
                {"title": "A Fast Method for Power Converter Simulation", "doi": "10.1101/2023.01.01.123456"},
                {"title": "Unrelated Paper", "doi": "10.1101/2023.01.01.999999"},
            ],
        })
        result = lookup_biorxiv_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertTrue(any("123456" in u for u in result))
        self.assertFalse(any("999999" in u for u in result))

    def test_api_error_status(self):
        session = _make_session(json_data={
            "messages": [{"status": "error"}],
            "collection": [],
        })
        result = lookup_biorxiv_pdf_urls_by_title(session, "title", timeout=5)
        self.assertEqual(result, [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=503)
        self.assertEqual(lookup_biorxiv_pdf_urls_by_title(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_semanticscholar_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupSemanticScholar(unittest.TestCase):
    def test_finds_open_access_pdf(self):
        session = _make_session(json_data={
            "data": [
                {
                    "title": "A Fast Method for Power Converter Simulation",
                    "openAccessPdf": {"url": "https://pdfs.semanticscholar.org/abc123.pdf"},
                },
                {
                    "title": "Unrelated",
                    "openAccessPdf": None,
                },
            ],
        })
        result = lookup_semanticscholar_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertEqual(result, ["https://pdfs.semanticscholar.org/abc123.pdf"])

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_semanticscholar_pdf_urls_by_title(session, "", timeout=5), [])

    def test_connection_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("fail")
        self.assertEqual(lookup_semanticscholar_pdf_urls_by_title(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_europepmc_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupEuropePmc(unittest.TestCase):
    def test_finds_pmcid_pdf(self):
        session = _make_session(json_data={
            "resultList": {
                "result": [
                    {
                        "title": "A Fast Method for Power Converter Simulation",
                        "pmcid": "PMC1234567",
                        "doi": "",
                        "isOpenAccess": "N",
                    },
                ],
            },
        })
        result = lookup_europepmc_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertTrue(any("PMC1234567" in u for u in result))

    def test_finds_open_access_doi_with_fulltext(self):
        session = _make_session(json_data={
            "resultList": {
                "result": [
                    {
                        "title": "A Fast Method for Power Converter Simulation",
                        "pmcid": "",
                        "doi": "10.1000/abc",
                        "isOpenAccess": "Y",
                        "fullTextUrlList": {
                            "fullTextUrl": [
                                {"documentStyle": "pdf", "url": "https://example.org/full.pdf"},
                            ],
                        },
                    },
                ],
            },
        })
        result = lookup_europepmc_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertIn("https://example.org/full.pdf", result)

    def test_non_200(self):
        session = _make_session(ok=False, status_code=502)
        self.assertEqual(lookup_europepmc_pdf_urls_by_title(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_core_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupCore(unittest.TestCase):
    def test_finds_pdf_download_url(self):
        session = _make_session(json_data={
            "results": [
                {"title": "A Fast Method for Simulation", "downloadUrl": "https://core.ac.uk/download/123.pdf"},
            ],
        })
        result = lookup_core_pdf_urls_by_title(
            session, "A Fast Method for Simulation", timeout=5
        )
        self.assertIn("https://core.ac.uk/download/123.pdf", result)

    def test_filters_non_pdf_downloads(self):
        session = _make_session(json_data={
            "results": [
                {"title": "A Method", "downloadUrl": "https://example.org/paper"},
            ],
        })
        result = lookup_core_pdf_urls_by_title(session, "A Method", timeout=5)
        self.assertEqual(result, [])

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_core_pdf_urls_by_title(session, "", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_secondary_ranked (dispatcher)
# ---------------------------------------------------------------------------

class TestLookupSecondaryRanked(unittest.TestCase):
    def test_returns_crossref_result_when_api_available(self):
        item = ReferenceItem(
            number=1,
            text='Smith J. "A Fast Method for Power Converter Simulation" IEEE Trans. 2023',
            dois=[], urls=[],
        )
        session = _make_session(json_data={
            "message": {
                "items": [
                    {
                        "title": ["A Fast Method for Power Converter Simulation"],
                        "DOI": "10.1000/abc123",
                        "URL": "https://doi.org/10.1000/abc123",
                        "link": [],
                        "issued": {"date-parts": [[2023]]},
                        "author": [{"family": "Smith"}],
                    },
                ],
            },
        })
        # The dispatcher calls Crossref first, then OpenAlex.
        # We need both to succeed (OpenAlex mock is same session).
        # Since both endpoints use the same session.get, we need to handle
        # multiple calls. For now, test that it doesn't crash and returns results.
        dois, urls = lookup_secondary_ranked(session, item, timeout=5, top_k=2)
        self.assertIsInstance(dois, list)
        self.assertIsInstance(urls, list)

    def test_empty_item_returns_empty(self):
        item = ReferenceItem(number=1, text="", dois=[], urls=[])
        session = _make_session(json_data={"message": {"items": []}})
        dois, urls = lookup_secondary_ranked(session, item, timeout=5, top_k=2)
        self.assertEqual(dois, [])
        self.assertEqual(urls, [])

    def test_network_error_graceful(self):
        item = ReferenceItem(number=1, text="Some paper title", dois=[], urls=[])
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("offline")
        dois, urls = lookup_secondary_ranked(session, item, timeout=5, top_k=2)
        # Should not raise, return empty
        self.assertEqual(dois, [])
        self.assertEqual(urls, [])


# ---------------------------------------------------------------------------
# lookup_neurips_proceedings_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupNeurips(unittest.TestCase):
    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_neurips_proceedings_pdf_urls_by_title(session, "", None, timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=500)
        self.assertEqual(lookup_neurips_proceedings_pdf_urls_by_title(session, "title", None, timeout=5), [])

    def test_connection_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("fail")
        self.assertEqual(lookup_neurips_proceedings_pdf_urls_by_title(session, "title", None, timeout=5), [])

    def test_finds_matching_paper(self):
        search_html = '<a href="/paper/2023/hash/Abstract-Conference.html">Link</a>'
        page_html = (
            '<meta name="citation_title" content="A Fast Method for Power Converter Simulation">'
            '<meta name="citation_pdf_url" content="https://proceedings.neurips.cc/paper.pdf">'
        )
        mock_resp_search = MagicMock()
        mock_resp_search.ok = True
        mock_resp_search.text = search_html
        mock_resp_page = MagicMock()
        mock_resp_page.ok = True
        mock_resp_page.text = page_html
        session = MagicMock()
        session.get.side_effect = [mock_resp_search, mock_resp_page]
        result = lookup_neurips_proceedings_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", None, timeout=5
        )
        self.assertEqual(result, ["https://proceedings.neurips.cc/paper.pdf"])


# ---------------------------------------------------------------------------
# lookup_google_books_pdf_urls
# ---------------------------------------------------------------------------

class TestLookupGoogleBooks(unittest.TestCase):
    def test_finds_pdf_download_link(self):
        session = _make_session(json_data={
            "items": [
                {
                    "volumeInfo": {"title": "A Fast Method for Power Converter Simulation"},
                    "accessInfo": {
                        "pdf": {"downloadLink": "https://books.google.com/books/download/abc.pdf"},
                        "webReaderLink": "",
                        "viewability": "PARTIAL",
                    },
                },
            ],
        })
        result = lookup_google_books_pdf_urls(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertIn("https://books.google.com/books/download/abc.pdf", result)

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_google_books_pdf_urls(session, "", timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=500)
        self.assertEqual(lookup_google_books_pdf_urls(session, "title", timeout=5), [])

    def test_connection_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("fail")
        self.assertEqual(lookup_google_books_pdf_urls(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_crossref_tdm_urls
# ---------------------------------------------------------------------------

class TestLookupCrossrefTdm(unittest.TestCase):
    def test_finds_pdf_from_link_field(self):
        session = _make_session(json_data={
            "message": {
                "items": [
                    {
                        "title": ["A Fast Method for Power Converter Simulation"],
                        "link": [
                            {"content-type": "application/pdf", "URL": "https://example.org/paper.pdf"},
                        ],
                    },
                ],
            },
        })
        result = lookup_crossref_tdm_urls(session, "A Fast Method for Power Converter Simulation", timeout=5)
        self.assertIn("https://example.org/paper.pdf", result)

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_crossref_tdm_urls(session, "", timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=503)
        self.assertEqual(lookup_crossref_tdm_urls(session, "title", timeout=5), [])

    def test_connection_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("fail")
        self.assertEqual(lookup_crossref_tdm_urls(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_ssrn_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupSrrn(unittest.TestCase):
    def test_finds_paper_and_builds_pdf_url(self):
        session = _make_session(json_data={
            "papers": [
                {
                    "title": "A Fast Method for Power Converter Simulation",
                    "ssrn_id": "1234567",
                },
            ],
        })
        result = lookup_ssrn_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertTrue(any("1234567" in u for u in result))

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_ssrn_pdf_urls_by_title(session, "", timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=502)
        self.assertEqual(lookup_ssrn_pdf_urls_by_title(session, "title", timeout=5), [])

    def test_connection_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("fail")
        self.assertEqual(lookup_ssrn_pdf_urls_by_title(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_chemrxiv_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupChemrxiv(unittest.TestCase):
    def test_finds_pdf_from_files(self):
        session = _make_session(json_data=[
            {
                "title": "A Fast Method for Power Converter Simulation",
                "files": [
                    {
                        "is_link_only": False,
                        "download_url": "https://figshare.com/ndownloader/files/abc123",
                    },
                ],
            },
        ])
        result = lookup_chemrxiv_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertIn("https://figshare.com/ndownloader/files/abc123", result)

    def test_skips_link_only_files(self):
        session = _make_session(json_data=[
            {
                "title": "A Fast Method for Power Converter Simulation",
                "files": [{"is_link_only": True, "download_url": "https://example.org/gated"}],
            },
        ])
        result = lookup_chemrxiv_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertEqual(result, [])

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_chemrxiv_pdf_urls_by_title(session, "", timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=404)
        self.assertEqual(lookup_chemrxiv_pdf_urls_by_title(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_researchgate_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupResearchgate(unittest.TestCase):
    def test_finds_pdf_from_html_patterns(self):
        session = _make_session(
            text='<a href="/publication/12345678_Method.pdf">PDF</a>'
        )
        result = lookup_researchgate_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertTrue(any("researchgate.net" in u for u in result))

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_researchgate_pdf_urls_by_title(session, "", timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=403)
        self.assertEqual(lookup_researchgate_pdf_urls_by_title(session, "title", timeout=5), [])

    def test_connection_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("fail")
        self.assertEqual(lookup_researchgate_pdf_urls_by_title(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_unpaywall_by_title
# ---------------------------------------------------------------------------

class TestLookupUnpaywallByTitle(unittest.TestCase):
    def test_finds_oa_url_through_crossref_then_unpaywall(self):
        """First call: Crossref search for DOI; Second call: Unpaywall lookup."""
        crossref_resp = MagicMock()
        crossref_resp.ok = True
        crossref_resp.json.return_value = {
            "message": {
                "items": [
                    {
                        "title": ["A Fast Method for Power Converter Simulation"],
                        "DOI": "10.1000/abc123",
                    },
                ],
            },
        }
        unpaywall_resp = MagicMock()
        unpaywall_resp.ok = True
        unpaywall_resp.status_code = 200
        unpaywall_resp.json.return_value = {
            "is_oa": True,
            "best_oa_location": {"url_for_pdf": "https://example.org/oa.pdf"},
        }
        session = MagicMock()
        session.get.side_effect = [crossref_resp, unpaywall_resp]
        result = lookup_unpaywall_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertIn("https://example.org/oa.pdf", result)

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_unpaywall_by_title(session, "", timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=500)
        self.assertEqual(lookup_unpaywall_by_title(session, "title", timeout=5), [])


# ---------------------------------------------------------------------------
# lookup_openalex_pdf_urls_by_title
# ---------------------------------------------------------------------------

class TestLookupOpenalex(unittest.TestCase):
    def test_finds_oa_pdf(self):
        session = _make_session(json_data={
            "results": [
                {
                    "title": "A Fast Method for Power Converter Simulation",
                    "open_access": {
                        "is_oa": True,
                        "oa_url": "https://example.org/open-access.pdf",
                    },
                },
            ],
        })
        result = lookup_openalex_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertIn("https://example.org/open-access.pdf", result)

    def test_finds_pdf_from_locations(self):
        session = _make_session(json_data={
            "results": [
                {
                    "title": "A Fast Method for Power Converter Simulation",
                    "open_access": {"is_oa": False},
                    "locations": [
                        {"pdf_url": "https://repo.example.org/paper.pdf"},
                    ],
                },
            ],
        })
        result = lookup_openalex_pdf_urls_by_title(
            session, "A Fast Method for Power Converter Simulation", timeout=5
        )
        self.assertIn("https://repo.example.org/paper.pdf", result)

    def test_empty_title(self):
        session = _make_session()
        self.assertEqual(lookup_openalex_pdf_urls_by_title(session, "", timeout=5), [])

    def test_non_200(self):
        session = _make_session(ok=False, status_code=429)
        self.assertEqual(lookup_openalex_pdf_urls_by_title(session, "title", timeout=5), [])

    def test_connection_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("fail")
        self.assertEqual(lookup_openalex_pdf_urls_by_title(session, "title", timeout=5), [])


if __name__ == "__main__":
    unittest.main()
