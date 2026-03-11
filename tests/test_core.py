import unittest
from pathlib import Path
import json

import reference_tool as rt


class TestCore(unittest.TestCase):
    def test_title_match_score(self):
        a = "A fast method for power converter simulation"
        b = "Fast methods for simulation of power converters"
        s1 = rt.title_match_score(a, b)
        s2 = rt.title_match_score(b, a)
        self.assertGreaterEqual(s1, 0.0)
        self.assertLessEqual(s1, 1.0)
        self.assertAlmostEqual(s1, s2, places=6)
        self.assertGreater(s1, 0.1)

    def test_iter_candidate_urls_order(self):
        item = rt.ReferenceItem(number=1, text="t", dois=["10.1000/xyz"], urls=[
            "https://example.com/page",
            "https://host/content/pdf/abc.pdf",
            "https://ieeexplore.ieee.org/ielx?arnumber=123",
            "https://doi.org/10.1000/zzz",
        ])
        c = list(rt.iter_candidate_urls(item, use_doi=True))
        self.assertTrue(c[0].endswith(".pdf") or "stampdf" in c[0].lower())
        self.assertTrue(any(u.endswith("doi.org/10.1000/xyz") or u.endswith("/10.1000/xyz") for u in c))

    def test_generic_site_templates_expand(self):
        item = rt.ReferenceItem(number=1, text="A sample paper title", dois=["10.1000/xyz"], urls=[])
        sites = [
            "https://sci-hub.se/{doi}",
            "https://example.org/search?q={title_encoded}",
        ]
        c = list(rt.iter_candidate_urls_with_generic_sites(item, use_doi=True, generic_download_sites=sites))
        self.assertTrue(any("sci-hub.se/10.1000/xyz" in u for u in c))
        self.assertTrue(any("example.org/search?q=" in u for u in c))

    def test_config_loader_trailing_commas(self):
        tmp = Path("tests/tmp.json")
        obj = {"a": 1, "b": {"c": 2}}
        text = '{ "a": 1, "b": { "c": 2, }, }'
        try:
            tmp.write_text(text, encoding="utf-8")
            data = rt.load_config_file(tmp)
            self.assertEqual(data, obj)
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_config_loader_utf8_bom(self):
        tmp = Path("tests/tmp_bom.json")
        text = "\ufeff" + '{ "a": 1 }'
        try:
            tmp.write_text(text, encoding="utf-8")
            data = rt.load_config_file(tmp)
            self.assertEqual(data, {"a": 1})
        finally:
            if tmp.exists():
                tmp.unlink()


if __name__ == "__main__":
    unittest.main()
