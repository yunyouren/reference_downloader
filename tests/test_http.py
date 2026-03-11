import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from core.http import is_probably_pdf, parse_retry_after_seconds


class TestHttpHelpers(unittest.TestCase):
    def test_is_probably_pdf_with_leading_whitespace(self):
        self.assertTrue(is_probably_pdf(b"  \n\t%PDF-1.7 content"))

    def test_is_probably_pdf_false_for_non_pdf(self):
        self.assertFalse(is_probably_pdf(b"<html>not a pdf</html>"))

    def test_parse_retry_after_numeric_seconds(self):
        self.assertEqual(parse_retry_after_seconds("120"), 120.0)

    def test_parse_retry_after_decimal_seconds(self):
        self.assertEqual(parse_retry_after_seconds("1.5"), 1.5)

    def test_parse_retry_after_negative_seconds_clamped(self):
        self.assertEqual(parse_retry_after_seconds("-3"), 0.0)

    def test_parse_retry_after_non_finite_seconds(self):
        self.assertIsNone(parse_retry_after_seconds("nan"))
        self.assertIsNone(parse_retry_after_seconds("inf"))

    def test_parse_retry_after_http_date_in_past(self):
        self.assertEqual(parse_retry_after_seconds("Sun, 06 Nov 1994 08:49:37 GMT"), 0.0)

    def test_parse_retry_after_http_date_in_future(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=3)
        value = format_datetime(future)
        seconds = parse_retry_after_seconds(value)
        self.assertIsNotNone(seconds)
        assert seconds is not None
        self.assertGreaterEqual(seconds, 0.0)
        self.assertLessEqual(seconds, 5.0)


if __name__ == "__main__":
    unittest.main()
