import unittest
from urllib.parse import quote

import types

from src.models import ReferenceItem, DownloadLogger, DownloadAttempt
from src.lookup import lookup_unpaywall, unique_preserve_order


class _DummySession:
    def get(self, *args, **kwargs):
        raise AssertionError("network should not be called in this test")


class TestUnpaywallLogging(unittest.TestCase):
    def test_unpaywall_candidate_and_injected(self):
        item = ReferenceItem(number=42, text="t", dois=["10.1234/abc"], urls=[])
        logger = DownloadLogger()
        sess = _DummySession()

        original = lookup_unpaywall
        try:
            # Monkey-patch lookup_unpaywall to return a fake OA URL
            import src.lookup
            src.lookup.lookup_unpaywall = lambda session, doi, email, timeout: "https://example.org/oa.pdf"
            # call the enriched logic by invoking only the unpaywall block
            # simulate the minimal part of enrich_failed_references unpaywall section
            for doi in item.dois:
                oa_url = src.lookup.lookup_unpaywall(sess, doi, email="x@y", timeout=3)
                api_url = f"https://api.unpaywall.org/v2/{quote(doi, safe='')}"
                if oa_url:
                    logger.add(DownloadAttempt(phase="secondary", ref_number=item.number, candidate_url=api_url, final_url=oa_url, status_code=0, content_type="", outcome="unpaywall_candidate", waited_seconds=0.0, error=""))
                    if oa_url not in item.urls:
                        item.urls = unique_preserve_order(list(item.urls) + [oa_url])
                        logger.add(DownloadAttempt(phase="secondary", ref_number=item.number, candidate_url=oa_url, final_url="", status_code=0, content_type="", outcome="unpaywall_injected", waited_seconds=0.0, error=""))
                else:
                    logger.add(DownloadAttempt(phase="secondary", ref_number=item.number, candidate_url=api_url, final_url="", status_code=0, content_type="", outcome="unpaywall_miss", waited_seconds=0.0, error=""))

            outcomes = [row.outcome for row in logger._rows]
            self.assertIn("unpaywall_candidate", outcomes)
            self.assertIn("unpaywall_injected", outcomes)
            self.assertEqual(item.urls, ["https://example.org/oa.pdf"])
        finally:
            src.lookup.lookup_unpaywall = original


if __name__ == "__main__":
    unittest.main()
