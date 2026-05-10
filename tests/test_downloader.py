import unittest
import json
import tempfile
from http.cookiejar import LoadError
from pathlib import Path

from src.models import ReferenceItem
from src.downloader import apply_resume_state, load_cookies_txt, make_session


class TestApplyResumeState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_item(self, number, status="failed"):
        return ReferenceItem(number=number, text=f"Ref {number}", dois=[], urls=[], download_status=status)

    def test_no_state_file(self):
        refs = [self._make_item(1)]
        apply_resume_state(refs, self.out, self.out / "downloads")
        self.assertEqual(refs[0].download_status, "failed")

    def test_resumes_downloaded_pdf(self):
        (self.out / "downloads").mkdir(parents=True)
        pdf = self.out / "downloads" / "001_paper.pdf"
        pdf.write_text("fake pdf")

        state = [{"number": 1, "download_status": "downloaded_pdf", "downloaded_file": "001_paper.pdf", "note": "ok"}]
        (self.out / "references.json").write_text(json.dumps(state))

        refs = [self._make_item(1)]
        apply_resume_state(refs, self.out, self.out / "downloads")
        self.assertEqual(refs[0].download_status, "downloaded_pdf")
        self.assertEqual(refs[0].downloaded_file, "001_paper.pdf")

    def test_skips_if_file_missing(self):
        (self.out / "downloads").mkdir(parents=True)
        state = [{"number": 1, "download_status": "downloaded_pdf", "downloaded_file": "missing.pdf", "note": ""}]
        (self.out / "references.json").write_text(json.dumps(state))

        refs = [self._make_item(1)]
        apply_resume_state(refs, self.out, self.out / "downloads")
        self.assertEqual(refs[0].download_status, "failed")

    def test_resumes_saved_landing_url(self):
        landing = self.out / "downloads" / "landing_urls" / "001_landing.txt"
        landing.parent.mkdir(parents=True)
        landing.write_text("https://example.com")
        state = [{"number": 1, "download_status": "saved_landing_url", "downloaded_file": "landing_urls/001_landing.txt", "note": ""}]
        (self.out / "references.json").write_text(json.dumps(state))

        refs = [self._make_item(1)]
        apply_resume_state(refs, self.out, self.out / "downloads")
        self.assertEqual(refs[0].download_status, "saved_landing_url")

    def test_corrupt_json(self):
        (self.out / "references.json").write_text("{not valid")
        refs = [self._make_item(1)]
        apply_resume_state(refs, self.out, self.out / "downloads")
        self.assertEqual(refs[0].download_status, "failed")

    def test_finds_pdf_by_prefix(self):
        (self.out / "downloads").mkdir(parents=True)
        pdf = self.out / "downloads" / "001_renamed_paper.pdf"
        pdf.write_text("fake pdf")

        state = [{"number": 1, "download_status": "downloaded_pdf", "downloaded_file": "", "note": ""}]
        (self.out / "references.json").write_text(json.dumps(state))

        refs = [self._make_item(1)]
        apply_resume_state(refs, self.out, self.out / "downloads")
        self.assertEqual(refs[0].download_status, "downloaded_pdf")


class TestLoadCookiesTxt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_json_cookies(self):
        path = Path(self.tmp.name) / "cookies.txt"
        path.write_text(json.dumps([
            {
                "name": "session",
                "value": "abc123",
                "domain": ".example.com",
                "path": "/",
                "secure": True,
            },
        ]))
        jar = load_cookies_txt(path)
        self.assertGreater(len(jar), 0)

    def test_loads_netscape_cookies(self):
        path = Path(self.tmp.name) / "cookies.txt"
        path.write_text(
            "# Netscape HTTP Cookie File\n"
            ".example.com\tTRUE\t/\tFALSE\t1700000000\tsession\tabc123\n"
        )
        jar = load_cookies_txt(path)
        self.assertGreater(len(jar), 0)

    def test_skips_removed_cookies(self):
        path = Path(self.tmp.name) / "cookies.txt"
        path.write_text(json.dumps([
            {"name": "ok", "value": "val1", "domain": ".example.com"},
            {"name": "bad", "value": "_remove_", "domain": ".example.com"},
            {"name": "empty", "value": "", "domain": ".example.com"},
        ]))
        jar = load_cookies_txt(path)
        cookies = list(jar)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0].name, "ok")

    def test_corrupt_json_raises_load_error(self):
        path = Path(self.tmp.name) / "cookies.txt"
        path.write_text("[invalid json")
        with self.assertRaises(LoadError):
            load_cookies_txt(path)


class TestMakeSession(unittest.TestCase):
    def test_creates_session_with_headers(self):
        sess = make_session(pool_size=10, user_agent="TestAgent/1.0", cookies_jar=None)
        self.assertIn("User-Agent", sess.headers)
        self.assertEqual(sess.headers["User-Agent"], "TestAgent/1.0")

    def test_mounts_http_adapter(self):
        sess = make_session(pool_size=4, user_agent="test", cookies_jar=None)
        self.assertIn("http://", sess.adapters)
        self.assertIn("https://", sess.adapters)


if __name__ == "__main__":
    unittest.main()
