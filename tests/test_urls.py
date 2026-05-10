import unittest

from core.urls import normalize_candidate_url


class TestUrls(unittest.TestCase):
    def test_normalize_ieee_staging(self):
        u = "https://xplorestaging.ieee.org/document/1234567"
        self.assertEqual(normalize_candidate_url(u), "https://ieeexplore.ieee.org/document/1234567")

    def test_convert_elsevier_api_pii(self):
        u = "https://api.elsevier.com/content/article/PII:S1877050913005115?httpAccept=text/xml"
        self.assertEqual(normalize_candidate_url(u), "https://linkinghub.elsevier.com/retrieve/pii/S1877050913005115")

    def test_elsevier_pii_path_format(self):
        u = "https://api.elsevier.com/content/pii/S1234567890"
        self.assertEqual(normalize_candidate_url(u), "https://linkinghub.elsevier.com/retrieve/pii/S1234567890")

    def test_empty_url(self):
        self.assertEqual(normalize_candidate_url(""), "")

    def test_non_special_url_passthrough(self):
        u = "https://arxiv.org/pdf/2301.00001.pdf"
        self.assertEqual(normalize_candidate_url(u), u)

    def test_none_input(self):
        self.assertEqual(normalize_candidate_url(None), "")


if __name__ == "__main__":
    unittest.main()
