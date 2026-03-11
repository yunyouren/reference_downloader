import json
from pathlib import Path

from reference_tool_gui import (
    load_gui_config_payload,
    load_summary_from_output,
    run_rename_only_on_output,
    summarize_references_payload,
)


def test_summarize_references_payload_counts() -> None:
    rows = [
        {"download_status": "downloaded_pdf", "note": ""},
        {"download_status": "saved_landing_url", "note": ""},
        {"download_status": "failed", "note": "resolved_by=secondary_lookup"},
        {"download_status": "unknown_status", "note": ""},
        {},
    ]
    summary = summarize_references_payload(rows)
    assert summary["total"] == 5
    assert summary["downloaded_pdf"] == 1
    assert summary["saved_landing_url"] == 1
    assert summary["failed"] == 1
    assert summary["not_attempted"] == 2
    assert summary["resolved_by_secondary_lookup"] == 1


def test_load_summary_from_output_missing_file(tmp_path: Path) -> None:
    summary = load_summary_from_output(tmp_path)
    assert summary["total"] == 0
    assert summary["downloaded_pdf"] == 0


def test_load_summary_from_output_valid_json(tmp_path: Path) -> None:
    refs = [
        {"download_status": "downloaded_pdf", "note": ""},
        {"download_status": "failed", "note": ""},
    ]
    (tmp_path / "references.json").write_text(json.dumps(refs), encoding="utf-8")
    summary = load_summary_from_output(tmp_path)
    assert summary["total"] == 2
    assert summary["downloaded_pdf"] == 1
    assert summary["failed"] == 1


def test_load_gui_config_payload_jsonc_and_bom(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.json"
    text = "\ufeff" + '{\n  "input": "a.pdf", // comment\n  "workers": 4,\n}\n'
    cfg.write_text(text, encoding="utf-8")
    data = load_gui_config_payload(cfg)
    assert data["input"] == "a.pdf"
    assert data["workers"] == 4


def test_run_rename_only_on_output_skips_non_pdf_entries(tmp_path: Path) -> None:
    out = tmp_path / "out"
    downloads = out / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    refs = [
        {"number": 1, "text": "A. Test, 2020", "download_status": "downloaded_pdf", "downloaded_file": "001.txt", "note": ""},
        {"number": 2, "text": "B. Test, 2021", "download_status": "failed", "downloaded_file": "", "note": ""},
    ]
    (out / "references.json").write_text(json.dumps(refs), encoding="utf-8")
    (downloads / "001.txt").write_text("x", encoding="utf-8")
    stats = run_rename_only_on_output(output_dir=out, verify_threshold=0.5, rename_mode="number_only")
    assert stats["processed"] == 0
    assert stats["skipped"] >= 2
