import unittest

from core.http import should_record_landing_url


class TestLandingPolicy(unittest.TestCase):
    def test_should_record_403_html(self):
        self.assertTrue(should_record_landing_url(403, "text/html; charset=UTF-8"))

    def test_should_not_record_404_html(self):
        self.assertFalse(should_record_landing_url(404, "text/html; charset=UTF-8"))

    def test_should_not_record_403_pdf(self):
        self.assertFalse(should_record_landing_url(403, "application/pdf"))


if __name__ == "__main__":
    unittest.main()
