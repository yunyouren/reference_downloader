import unittest
import json
import tempfile
from pathlib import Path

from src.models import ReferenceItem
from src.downloader import apply_resume_state


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


if __name__ == "__main__":
    unittest.main()
