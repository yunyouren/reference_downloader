import unittest
from core.html import extract_springer_pdf_url, extract_ieee_arnumber, extract_ieee_pdf_url


class TestHtmlParsers(unittest.TestCase):
    def test_extract_springer_pdf_url_meta(self):
        html = '<meta name="citation_pdf_url" content="/content/pdf/10.1007_s00521-020-05387-9.pdf">'
        base = "https://link.springer.com/article/10.1007/s00521-020-05387-9"
        url = extract_springer_pdf_url(html, base)
        self.assertTrue(url.endswith(".pdf"))
        self.assertIn("link.springer.com/content/pdf", url)

    def test_extract_springer_pdf_url_fallback(self):
        html = "<html></html>"
        base = "https://link.springer.com/article/10.1007/s00521-020-05387-9"
        url = extract_springer_pdf_url(html, base)
        self.assertTrue(url.endswith(".pdf"))

    def test_extract_ieee_arnumber(self):
        url = extract_ieee_arnumber("https://ieeexplore.ieee.org/document/1234567")
        self.assertEqual(url, "1234567")
        self.assertIsNone(extract_ieee_arnumber("https://example.com/other"))

    def test_extract_ieee_pdf_url(self):
        ar = "1234567"
        base = "https://ieeexplore.ieee.org/document/1234567"
        html_iframe = f'<iframe src="/stampPDF/getPDF.jsp?tp=&arnumber={ar}&tag=1"></iframe>'
        pdf_url = extract_ieee_pdf_url(html_iframe, base_url=base, arnumber=ar)
        self.assertIn("stampPDF/getPDF.jsp", pdf_url)
        self.assertIn(ar, pdf_url)


if __name__ == "__main__":
    unittest.main()
