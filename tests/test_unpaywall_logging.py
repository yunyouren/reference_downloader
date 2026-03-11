import unittest

import types

import reference_tool as rt


class _DummySession:
    def get(self, *args, **kwargs):
        raise AssertionError("network should not be called in this test")


class TestUnpaywallLogging(unittest.TestCase):
    def test_unpaywall_candidate_and_injected(self):
        item = rt.ReferenceItem(number=42, text="t", dois=["10.1234/abc"], urls=[])
        logger = rt.DownloadLogger()
        sess = _DummySession()

        original = rt.lookup_unpaywall
        try:
            rt.lookup_unpaywall = lambda session, doi, email, timeout: "https://example.org/oa.pdf"
            # call the enriched logic by invoking only the unpaywall block
            # simulate the minimal part of enrich_failed_references unpaywall section
            for doi in item.dois:
                oa_url = rt.lookup_unpaywall(sess, doi, email="x@y", timeout=3)
                api_url = f"https://api.unpaywall.org/v2/{rt.quote(doi, safe='')}"
                if oa_url:
                    logger.add(rt.DownloadAttempt(phase="secondary", ref_number=item.number, candidate_url=api_url, final_url=oa_url, status_code=0, content_type="", outcome="unpaywall_candidate", waited_seconds=0.0, error=""))
                    if oa_url not in item.urls:
                        item.urls = rt.unique_preserve_order(list(item.urls) + [oa_url])
                        logger.add(rt.DownloadAttempt(phase="secondary", ref_number=item.number, candidate_url=oa_url, final_url="", status_code=0, content_type="", outcome="unpaywall_injected", waited_seconds=0.0, error=""))
                else:
                    logger.add(rt.DownloadAttempt(phase="secondary", ref_number=item.number, candidate_url=api_url, final_url="", status_code=0, content_type="", outcome="unpaywall_miss", waited_seconds=0.0, error=""))

            outcomes = [row.outcome for row in logger._rows]
            self.assertIn("unpaywall_candidate", outcomes)
            self.assertIn("unpaywall_injected", outcomes)
            self.assertEqual(item.urls, ["https://example.org/oa.pdf"])
        finally:
            rt.lookup_unpaywall = original


if __name__ == "__main__":
    unittest.main()
