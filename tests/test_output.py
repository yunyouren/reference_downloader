import unittest
import json
import tempfile
from pathlib import Path

from src.models import ReferenceItem
from src.output import write_outputs


class TestWriteOutputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "output"
        self.refs = [
            ReferenceItem(number=1, text="Smith, J. A fast method.", dois=["10.1000/abc"], urls=["https://example.com/1.pdf"], download_status="downloaded_pdf", downloaded_file="downloads/001_smith.pdf"),
            ReferenceItem(number=2, text="Brown, T. Grid stability.", dois=["10.1000/def", "10.1000/ghi"], urls=["https://example.com/2", "https://example.com/2.pdf"], download_status="failed", note="404"),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_markdown_file(self):
        write_outputs(self.refs, self.out)
        md = self.out / "numbered_references.md"
        self.assertTrue(md.exists())
        content = md.read_text(encoding="utf-8")
        self.assertIn("Smith, J.", content)
        self.assertIn("Brown, T.", content)
        self.assertIn("📄", content)  # status icon for downloaded pdf

    def test_creates_json_file(self):
        write_outputs(self.refs, self.out)
        jf = self.out / "references.json"
        self.assertTrue(jf.exists())
        data = json.loads(jf.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["number"], 1)
        self.assertEqual(data[0]["download_status"], "downloaded_pdf")

    def test_creates_csv_file(self):
        write_outputs(self.refs, self.out)
        cf = self.out / "references.csv"
        self.assertTrue(cf.exists())
        content = cf.read_text(encoding="utf-8")
        self.assertIn("number,text,dois,urls", content)
        self.assertIn("10.1000/abc", content)
        self.assertIn("10.1000/def; 10.1000/ghi", content)

    def test_creates_output_dir_if_missing(self):
        sub = self.out / "sub" / "deep"
        write_outputs(self.refs, sub)
        self.assertTrue((sub / "numbered_references.md").exists())

    def test_empty_refs(self):
        write_outputs([], self.out)
        md = self.out / "numbered_references.md"
        content = md.read_text(encoding="utf-8")
        self.assertIn("# Numbered References", content)
        data = json.loads((self.out / "references.json").read_text(encoding="utf-8"))
        self.assertEqual(data, [])


if __name__ == "__main__":
    unittest.main()
