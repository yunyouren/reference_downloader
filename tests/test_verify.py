import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.verify import build_verified_pdf_name, verify_and_rename_pdf


class _DummyPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self):
        return self._text


class _DummyReader:
    def __init__(self, _path: str, *, title: str, page_text: str) -> None:
        self.metadata = {"/Title": title}
        self.pages = [_DummyPage(page_text)]


class TestVerify(unittest.TestCase):
    def test_build_verified_pdf_name_modes(self):
        self.assertEqual(
            build_verified_pdf_name(prefix="001", original_name="My Paper Title", rename_mode="original"),
            "My Paper Title.pdf",
        )
        self.assertEqual(
            build_verified_pdf_name(prefix="001", original_name="My Paper Title", rename_mode="number_only"),
            "001.pdf",
        )
        self.assertEqual(
            build_verified_pdf_name(prefix="001", original_name="My Paper Title", rename_mode="number_and_original"),
            "001 My Paper Title.pdf",
        )

    def test_verify_and_rename_pdf_success(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            downloads_dir = base / "downloads"
            verified_dir = downloads_dir / "verified_pdfs"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            out_file = downloads_dir / "001.pdf"
            out_file.write_bytes(b"not-a-real-pdf")

            def reader_cls(path: str):
                return _DummyReader(path, title="My Paper Title", page_text="My Paper Title\nSmith 2020\n")

            decision = verify_and_rename_pdf(
                prefix="001",
                out_file=out_file,
                downloads_dir=downloads_dir,
                verified_dir=verified_dir,
                mismatch_dir=downloads_dir / "mismatch_pdfs",
                expected_title="My Paper Title",
                ref_year=2020,
                surname="smith",
                verify_title_threshold=0.2,
                verify_weights=None,
                reader_cls=reader_cls,
            )
            self.assertEqual(decision.outcome, "downloaded_pdf")
            self.assertTrue(decision.file_path.exists())
            self.assertTrue(decision.file_path.name.startswith("001 "))
            self.assertIn("verified_pdfs", decision.rel_path)
            self.assertFalse((downloads_dir / "001.pdf").exists())

    def test_verify_and_rename_pdf_mismatch(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            downloads_dir = base / "downloads"
            mismatch_dir = downloads_dir / "mismatch_pdfs"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            mismatch_dir.mkdir(parents=True, exist_ok=True)
            out_file = downloads_dir / "002.pdf"
            out_file.write_bytes(b"not-a-real-pdf")

            def reader_cls(path: str):
                return _DummyReader(path, title="Unrelated Title", page_text="Unrelated Title\n")

            decision = verify_and_rename_pdf(
                prefix="002",
                out_file=out_file,
                downloads_dir=downloads_dir,
                verified_dir=downloads_dir / "verified_pdfs",
                mismatch_dir=mismatch_dir,
                expected_title="Completely Different Expected Title",
                ref_year=None,
                surname="",
                verify_title_threshold=0.95,
                verify_weights=None,
                reader_cls=reader_cls,
            )
            self.assertEqual(decision.outcome, "pdf_title_mismatch")
            self.assertTrue(decision.file_path.exists())
            self.assertTrue(decision.file_path.name.startswith("002__mismatch"))
            self.assertFalse((downloads_dir / "002.pdf").exists())

    def test_verify_and_rename_pdf_number_only_mode(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            downloads_dir = base / "downloads"
            verified_dir = downloads_dir / "verified_pdfs"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            out_file = downloads_dir / "003.pdf"
            out_file.write_bytes(b"not-a-real-pdf")

            def reader_cls(path: str):
                return _DummyReader(path, title="My Paper Title", page_text="My Paper Title\nSmith 2020\n")

            decision = verify_and_rename_pdf(
                prefix="003",
                out_file=out_file,
                downloads_dir=downloads_dir,
                verified_dir=verified_dir,
                mismatch_dir=downloads_dir / "mismatch_pdfs",
                expected_title="My Paper Title",
                ref_year=2020,
                surname="smith",
                verify_title_threshold=0.2,
                verify_weights=None,
                verify_rename_mode="number_only",
                reader_cls=reader_cls,
            )
            self.assertEqual(decision.outcome, "downloaded_pdf")
            self.assertTrue(decision.file_path.name == "003.pdf")


if __name__ == "__main__":
    unittest.main()
