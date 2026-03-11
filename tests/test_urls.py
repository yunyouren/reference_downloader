import unittest

from core.urls import normalize_candidate_url


class TestUrls(unittest.TestCase):
    def test_normalize_ieee_staging(self):
        u = "https://xplorestaging.ieee.org/document/1234567"
        self.assertEqual(normalize_candidate_url(u), "https://ieeexplore.ieee.org/document/1234567")

    def test_convert_elsevier_api_pii(self):
        u = "https://api.elsevier.com/content/article/PII:S1877050913005115?httpAccept=text/xml"
        self.assertEqual(normalize_candidate_url(u), "https://linkinghub.elsevier.com/retrieve/pii/S1877050913005115")


if __name__ == "__main__":
    unittest.main()
