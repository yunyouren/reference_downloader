"""Offline regression tests: synthetic PDFs/credentials, never publisher traffic."""
import inspect
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.cookiejar import MozillaCookieJar
from unittest.mock import Mock

import pytest
import requests
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

import download_paper as single
from src import downloader as dl
from src.models import DownloadLogger, DomainLimiter, PipelineConfig, ReferenceItem
from src.output import write_outputs
from src.parsers import cleanup_reference_text, parse_numeric_references
from core.pdf_files import is_readable_pdf, save_pdf_stream


def pdf_bytes(title="Offline test paper"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject()
    body = "This paper presents power conversion experiments and validates the proposed converter design. " * 6
    content.set_data(("BT /F1 12 Tf 40 700 Td (" + title + ") Tj 0 -20 Td (1. Introduction) Tj 0 -20 Td (" + body + ") Tj ET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.add_metadata({"/Title": title})
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def response(content, url="https://example.test/paper.pdf", content_type="application/pdf"):
    result = Mock(ok=True, status_code=200, url=url, content=content,
                  headers={"content-type": content_type})
    result.iter_content.side_effect = lambda **kwargs: iter([content])
    return result


def phase_kwargs(fn, refs, folder):
    defaults = dict(refs=refs, downloads_dir=folder, meta_dir=None, landing_dir=None,
        mismatch_dir=folder, timeout=1, lookup_timeout=1, retries=1, use_doi=True,
        max_candidates_per_item=3, max_items=0, secondary_top_k=1, workers=1,
        show_progress=False, user_agent="OfflineTest", max_per_domain=1,
        min_domain_delay_ms=0, logger=DownloadLogger(), cookies_jar=None,
        verify_title_rename=False, verify_title_threshold=.65,
        verify_rename_mode="number_and_original", verify_weights=None,
        verified_dir=None, secondary_cache=None, neurips_proceedings=False)
    return {name: defaults[name] for name, param in inspect.signature(fn).parameters.items()
            if param.default is inspect.Parameter.empty}


@pytest.mark.parametrize("body,ct", [
    (b"<html>Login required</html>", "text/html"),
    (b"<html>Login required</html>", "application/pdf"),
    (b"%PDF-1.7\ntruncated", "application/pdf"),
])
def test_single_does_not_accept_fake_pdf(body, ct):
    session = Mock()
    session.get.return_value = response(body, content_type=ct)
    content, _, _ = single._try_fetch_pdf(session, "https://example.test/paper.pdf", 1)
    assert content is None
    session.get.return_value.close.assert_called_once()


def test_single_accepts_real_pdf_without_pdf_headers():
    body = pdf_bytes()
    session = Mock()
    session.get.return_value = response(body, "https://example.test/download", "application/octet-stream")
    content, _, _ = single._try_fetch_pdf(session, "https://example.test/download", 1)
    assert content == body


def test_single_never_overwrites_previous_paper(tmp_path):
    first, second = pdf_bytes("First paper"), pdf_bytes("Second paper")
    session = Mock()
    session.get.side_effect = [response(first, "https://example.test/a"),
                               response(second, "https://example.test/b")]
    one = single.download_paper(session, ["https://example.test/a"], tmp_path, 1)
    two = single.download_paper(session, ["https://example.test/b"], tmp_path, 1)
    assert one != two
    assert one.read_bytes() == first
    assert two.read_bytes() == second


def make_state(tmp_path, old, body=None, filename="001_old.pdf"):
    folder = tmp_path / "downloads"
    folder.mkdir(exist_ok=True)
    if body is not None:
        (folder / filename).write_bytes(body)
    (tmp_path / "references.json").write_text(json.dumps([old]), encoding="utf-8")
    return folder


def state_row(text="Same paper", dois=None, **changes):
    return dict(number=1, text=text, dois=dois or ["10.1234/same"], urls=[],
                download_status="downloaded_pdf", downloaded_file="001_old.pdf", **changes)


def test_resume_rejects_different_paper_with_same_number(tmp_path):
    folder = make_state(tmp_path, state_row("Old paper", ["10.1234/old"]), pdf_bytes())
    item = ReferenceItem(1, "New paper", dois=["10.1234/new"])
    dl.apply_resume_state([item], tmp_path, folder)
    assert item.download_status != "downloaded_pdf"


def test_resume_same_identity_and_real_pdf(tmp_path):
    folder = make_state(tmp_path, state_row(), pdf_bytes())
    item = ReferenceItem(1, "Same paper", dois=["https://doi.org/10.1234/SAME"])
    dl.apply_resume_state([item], tmp_path, folder)
    assert item.download_status == "downloaded_pdf"


def test_resume_rejects_invalid_file(tmp_path):
    folder = make_state(tmp_path, state_row(), b"<html>not a PDF</html>")
    item = ReferenceItem(1, "Same paper", dois=["10.1234/same"])
    dl.apply_resume_state([item], tmp_path, folder)
    assert item.download_status != "downloaded_pdf"


def test_resume_does_not_trust_number_only_legacy_state(tmp_path):
    row = {"number": 1, "download_status": "downloaded_pdf", "downloaded_file": "001_old.pdf"}
    folder = make_state(tmp_path, row, pdf_bytes())
    item = ReferenceItem(1, "Same paper")
    dl.apply_resume_state([item], tmp_path, folder)
    assert item.download_status != "downloaded_pdf"


def test_resume_detects_changed_pdf_when_hash_available(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "001_old.pdf").write_bytes(pdf_bytes("First file"))
    old = ReferenceItem(1, "Same paper", dois=["10.1234/same"],
                        download_status="downloaded_pdf", downloaded_file="001_old.pdf")
    write_outputs([old], tmp_path)
    (folder / "001_old.pdf").write_bytes(pdf_bytes("Changed file"))
    new = ReferenceItem(1, "Same paper", dois=["10.1234/same"])
    dl.apply_resume_state([new], tmp_path, folder)
    assert new.download_status != "downloaded_pdf"


def test_landing_only_item_is_retried(tmp_path):
    item = ReferenceItem(1, "Paper", urls=["https://example.test/paper.pdf"],
                         download_status="saved_landing_url", downloaded_file="001_landing.txt")
    (tmp_path / item.downloaded_file).write_text("https://example.test/paper.pdf")
    session = Mock()
    session.get.return_value = response(pdf_bytes())
    kw = phase_kwargs(dl.run_initial_download_phase, [item], tmp_path)
    kw.pop("refs"); kw.pop("workers"); kw.pop("show_progress"); kw.pop("user_agent")
    kw.pop("max_per_domain"); kw.pop("min_domain_delay_ms"); kw.pop("cookies_jar")
    dl.try_download(session=session, item=item, domain_limiter=DomainLimiter(1, 0), phase="initial", **kw)
    assert item.download_status == "downloaded_pdf"
    assert session.get.called


def mock_secondary_sources(monkeypatch, ranked=None):
    monkeypatch.setattr(dl, "lookup_secondary_ranked", ranked or Mock(return_value=([], [])))
    for name in (
        "lookup_neurips_proceedings_pdf_urls_by_title", "lookup_arxiv_pdf_urls_by_title",
        "lookup_semanticscholar_pdf_urls_by_title", "lookup_europepmc_pdf_urls_by_title",
        "lookup_biorxiv_pdf_urls_by_title", "lookup_core_pdf_urls_by_title",
        "lookup_openalex_pdf_urls_by_title", "lookup_crossref_tdm_urls",
        "lookup_google_books_pdf_urls", "lookup_ssrn_pdf_urls_by_title",
        "lookup_chemrxiv_pdf_urls_by_title", "lookup_researchgate_pdf_urls_by_title",
        "lookup_unpaywall_by_title",
    ):
        monkeypatch.setattr(dl, name, Mock(return_value=[]))
    monkeypatch.setattr(dl, "lookup_unpaywall", Mock(return_value=None))


def test_secondary_includes_landing_only_items(tmp_path, monkeypatch):
    ranked = Mock(return_value=([], []))
    mock_secondary_sources(monkeypatch, ranked)
    monkeypatch.setattr(dl, "try_download", Mock())
    item = ReferenceItem(1, "A paper title", download_status="saved_landing_url")
    dl.enrich_failed_references(**phase_kwargs(dl.enrich_failed_references, [item], tmp_path))
    assert ranked.called
    assert "worker error" not in item.note


def test_secondary_worker_exception_is_recorded(tmp_path, monkeypatch):
    mock_secondary_sources(monkeypatch, Mock(side_effect=RuntimeError("Synthetic lookup failure")))
    item = ReferenceItem(1, "A paper title", download_status="failed")
    kw = phase_kwargs(dl.enrich_failed_references, [item], tmp_path)
    dl.enrich_failed_references(**kw)
    assert "Synthetic lookup failure" in item.note
    assert any(r.outcome == "lookup_exception" for r in kw["logger"]._rows)


def synthetic_cookie_jar():
    jar = MozillaCookieJar()
    jar.set_cookie(requests.cookies.create_cookie("test_session", "not-a-real-secret",
        domain="ieeexplore.ieee.org", secure=True))
    return jar


@pytest.mark.parametrize("phase", ["initial", "secondary"])
def test_doi_only_domain_cookie_is_available_and_scoped(tmp_path, monkeypatch, phase):
    observed = []
    def capture(**kwargs):
        session = kwargs["session"]
        observed.append(session.prepare_request(requests.Request("GET", "https://ieeexplore.ieee.org/document/1")))
        observed.append(session.prepare_request(requests.Request("GET", "https://api.crossref.org/works")))
        observed.append(session.prepare_request(requests.Request("GET", "http://ieeexplore.ieee.org/document/1")))
    monkeypatch.setattr(dl, "try_download", capture)
    item = ReferenceItem(1, "A paper", dois=["10.1109/TPEL.2025.1234567"], download_status="failed")
    fn = dl.run_initial_download_phase if phase == "initial" else dl.enrich_failed_references
    if phase == "secondary":
        mock_secondary_sources(monkeypatch)
    kw = phase_kwargs(fn, [item], tmp_path)
    kw["domain_cookies"] = {"ieeexplore.ieee.org": synthetic_cookie_jar()}
    fn(**kw)
    assert "test_session=" in observed[0].headers.get("Cookie", "")
    assert "Cookie" not in observed[1].headers
    assert "Cookie" not in observed[2].headers


def test_interactive_cookie_config_save_does_not_replace_dict(tmp_path, monkeypatch):
    from src import interactive_ui
    monkeypatch.setattr(interactive_ui, "display_domain_summary", lambda *_: None)
    expected = {"ieeexplore.ieee.org": {"cookies_path": "synthetic-missing-file"}}
    monkeypatch.setattr(interactive_ui, "configure_cookies_interactively", lambda *_: expected)
    load = Mock(return_value={})
    monkeypatch.setattr(dl, "load_domain_cookies", load)
    cfg = PipelineConfig(input_pdf=tmp_path / "unused.pdf", output_dir=tmp_path,
        no_download=True, interactive="true", domain_cookies_config={}, resume=False)
    dl.run_pipeline_from_refs([], cfg)
    assert load.call_args.args[0] == expected


def test_wrapped_doi_keeps_semantic_hyphen():
    doi = "10.23919/IPEC-Himeji2022-ECCE53331.2022.9807028"
    refs = parse_numeric_references("[1] Author. doi: " + doi.replace("IPEC-", "IPEC-\n"))
    assert refs[0].dois == [doi]
    assert cleanup_reference_text("contin-\nued") == "continued"


@pytest.mark.parametrize("text,expected", [
    ("doi:10.1234/foo-\nbar", "doi:10.1234/foo-bar"),
    ("doi:10.1234/\nfoo-bar", "doi:10.1234/foo-bar"),
    ("https://example.test/foo-\nbar.pdf", "https://example.test/foo-bar.pdf"),
    ("https://example.test/paper.pdf.\nAuthor", "https://example.test/paper.pdf. Author"),
    ("doi:10.1234/foo.\nNext citation", "doi:10.1234/foo. Next citation"),
    ("https://example.test/a.pdf\ncontin-\nued", "https://example.test/a.pdf continued"),
])
def test_identifier_cleanup_does_not_absorb_prose(text, expected):
    assert cleanup_reference_text(text) == expected


@pytest.mark.parametrize("filename", ["../outside.pdf", "001_missing.pdf"])
def test_resume_rejects_outside_or_missing_file(tmp_path, filename):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf_bytes())
    row = state_row()
    row["downloaded_file"] = filename
    folder = make_state(tmp_path, row)
    item = ReferenceItem(1, "Same paper", dois=["10.1234/same"])
    dl.apply_resume_state([item], tmp_path, folder)
    assert item.download_status != "downloaded_pdf"


def test_resume_without_doi_requires_same_text(tmp_path):
    row = state_row()
    row["dois"] = []
    folder = make_state(tmp_path, row, pdf_bytes())
    matching, other = ReferenceItem(1, "Same  paper"), ReferenceItem(1, "Different paper")
    dl.apply_resume_state([matching], tmp_path, folder)
    dl.apply_resume_state([other], tmp_path, folder)
    assert matching.download_status == "downloaded_pdf"
    assert other.download_status != "downloaded_pdf"


def test_save_stream_concurrent_names_are_unique(tmp_path):
    contents = [pdf_bytes(f"Paper {i}") for i in range(12)]
    with ThreadPoolExecutor(max_workers=6) as executor:
        paths = list(executor.map(lambda body: save_pdf_stream(tmp_path / "001.pdf", [body]), contents))
    assert len(set(paths)) == len(contents)
    assert [p.read_bytes() for p in paths] == contents
    assert not list(tmp_path.glob("*.part"))


def test_invalid_stream_preserves_old_file_and_cleans_temporary(tmp_path):
    original = pdf_bytes()
    (tmp_path / "001.pdf").write_bytes(original)
    assert save_pdf_stream(tmp_path / "001.pdf", [b"%PDF-1.7\nbroken"]) is None
    assert (tmp_path / "001.pdf").read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["001.pdf"]


def test_stream_failure_cleans_temporary_without_touching_old_file(tmp_path):
    original = pdf_bytes()
    (tmp_path / "001.pdf").write_bytes(original)
    def broken():
        yield b"%PDF-1.7"
        raise requests.ConnectionError("Offline interrupted stream")
    with pytest.raises(requests.ConnectionError):
        save_pdf_stream(tmp_path / "001.pdf", broken())
    assert (tmp_path / "001.pdf").read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["001.pdf"]


def test_failed_publication_cleans_its_reservation(tmp_path, monkeypatch):
    original = pdf_bytes("Existing PDF")
    destination = tmp_path / "001.pdf"
    destination.write_bytes(original)
    monkeypatch.setattr("core.pdf_files._move_exclusive", Mock(side_effect=OSError("Offline publish failure")))
    with pytest.raises(OSError):
        save_pdf_stream(destination, [pdf_bytes("New PDF")])
    assert destination.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["001.pdf"]


def test_pdf_without_pages_or_with_password_is_not_readable():
    empty = PdfWriter(); empty_stream = io.BytesIO(); empty.write(empty_stream)
    assert not is_readable_pdf(empty_stream.getvalue())
    encrypted = PdfWriter(); encrypted.add_blank_page(width=600, height=800)
    encrypted.encrypt("synthetic-password")
    encrypted_stream = io.BytesIO(); encrypted.write(encrypted_stream)
    assert not is_readable_pdf(encrypted_stream.getvalue())


@pytest.mark.parametrize("phase", ["initial", "secondary"])
def test_one_worker_error_does_not_discard_other_downloads(tmp_path, monkeypatch, phase):
    items = [ReferenceItem(i, f"Paper {i}", urls=["https://example.test/a.pdf"], download_status="failed")
             for i in [1, 2]]
    def attempt(**kwargs):
        item = kwargs["item"]
        if item.number == 1:
            raise RuntimeError("Synthetic item failure")
        item.download_status = "downloaded_pdf"
        item.downloaded_file = "002.pdf"
    monkeypatch.setattr(dl, "try_download", attempt)
    if phase == "secondary":
        mock_secondary_sources(monkeypatch)
    fn = dl.run_initial_download_phase if phase == "initial" else dl.enrich_failed_references
    kw = phase_kwargs(fn, items, tmp_path); kw["workers"] = 2
    fn(**kw)
    assert items[0].download_status == "failed"
    assert "Synthetic item failure" in items[0].note
    assert items[1].download_status == "downloaded_pdf"
    assert any(row.outcome == "worker_exception" for row in kw["logger"]._rows)


@pytest.mark.parametrize("site,mode", [
    ("springer", "direct"), ("chinese", "direct"), ("ieee", "stamp_pdf"), ("ieee", "stamp_html"),
])
@pytest.mark.parametrize("valid", [True, False])
def test_site_handlers_validate_pdf_and_preserve_existing_files(tmp_path, monkeypatch, site, mode, valid):
    from site_handlers.springer import handle_springer_html
    from site_handlers.ieee import handle_ieee_html
    from site_handlers.chinese_journals import handle_chinese_journal_html
    handlers = {"springer": handle_springer_html, "ieee": handle_ieee_html,
                "chinese": handle_chinese_journal_html}
    old_body = pdf_bytes("Existing paper")
    (tmp_path / "001.pdf").write_bytes(old_body)
    new_body = pdf_bytes("New paper") if valid else b"%PDF-1.7\ntruncated"
    pdf_url = "https://example.test/new.pdf"
    helpers = dict(parse_retry_after_seconds=dl.parse_retry_after_seconds,
        is_probably_pdf=dl.is_probably_pdf,
        verify_downloaded_pdf_and_update_item=dl.verify_downloaded_pdf_and_update_item,
        extract_springer_pdf_url=lambda *a, **k: pdf_url,
        extract_chinese_journal_pdf_url=lambda *a, **k: pdf_url,
        extract_ieee_arnumber=lambda *a, **k: "1234567",
        extract_ieee_pdf_url=lambda *a, **k: pdf_url,
        DownloadAttempt=dl.DownloadAttempt)
    session = Mock()
    responses = [response(new_body, pdf_url)]
    if mode == "stamp_html":
        responses.insert(0, response(b"<html>PDF iframe</html>", content_type="text/html"))
    session.get.side_effect = responses
    item = ReferenceItem(1, "New paper")
    result = handlers[site](session=session, item=item, helpers=helpers,
        downloads_dir=tmp_path, mismatch_dir=None, verified_dir=None, timeout=1,
        attempt=0, verify_title_rename=False, verify_title_threshold=.65,
        verify_rename_mode="number_and_original", verify_weights=None,
        logger=DownloadLogger(), phase="initial", seen=set(), prefix="001",
        final_url="https://example.test/article", first_chunk=b"<html></html>", chunks=iter([]))
    assert (tmp_path / "001.pdf").read_bytes() == old_body
    assert not list(tmp_path.glob("*.part"))
    if valid:
        assert result == "downloaded"
        assert (tmp_path / item.downloaded_file).read_bytes() == new_body
        assert item.downloaded_file != "001.pdf"
    else:
        assert result != "downloaded"
        assert item.download_status != "downloaded_pdf"
    for resp in responses:
        resp.close.assert_called_once()


def test_cookie_redirect_routing_uses_real_requests_without_network():
    class OfflineAdapter(requests.adapters.BaseAdapter):
        def __init__(self):
            self.sent = []
        def send(self, request, **kwargs):
            self.sent.append(request)
            result = requests.Response()
            result.request = request; result.url = request.url
            result._content = b""
            if len(self.sent) == 1:
                result.status_code = 302
                result.headers["Location"] = "https://ieeexplore.ieee.org/document/1"
            else:
                result.status_code = 200
            return result
        def close(self):
            pass
    jar = dl.merge_cookie_jars(None, {"ieeexplore.ieee.org": synthetic_cookie_jar()})
    with dl.make_session(4, "OfflineTest", jar) as session:
        adapter = OfflineAdapter(); session.mount("https://", adapter)
        session.get("https://doi.org/10.1109/test")
    assert "Cookie" not in adapter.sent[0].headers
    assert "test_session=" in adapter.sent[1].headers.get("Cookie", "")


def test_pipeline_passes_domain_cookies_to_both_phases(tmp_path, monkeypatch):
    expected = {"ieeexplore.ieee.org": synthetic_cookie_jar()}
    monkeypatch.setattr(dl, "load_domain_cookies", Mock(return_value=expected))
    initial, secondary = Mock(), Mock()
    monkeypatch.setattr(dl, "run_initial_download_phase", initial)
    monkeypatch.setattr(dl, "enrich_failed_references", secondary)
    cfg = PipelineConfig(input_pdf=tmp_path / "unused.pdf", output_dir=tmp_path,
        interactive="false", domain_cookies_config={}, resume=False, secondary_lookup=True)
    dl.run_pipeline_from_refs([], cfg)
    assert initial.call_args.kwargs["domain_cookies"] is expected
    assert secondary.call_args.kwargs["domain_cookies"] is expected


def test_atomic_manifest_failure_preserves_previous_state(tmp_path, monkeypatch):
    old = ReferenceItem(1, "Previous reference")
    write_outputs([old], tmp_path)
    previous = (tmp_path / "references.json").read_bytes()
    original_replace = Path.replace
    def fail_json(source, target):
        if Path(target).name == "references.json":
            raise OSError("Offline state-write failure")
        return original_replace(source, target)
    monkeypatch.setattr(Path, "replace", fail_json)
    with pytest.raises(OSError):
        write_outputs([ReferenceItem(1, "New reference")], tmp_path)
    assert (tmp_path / "references.json").read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))


def test_single_cli_rejects_html_and_returns_failure(tmp_path, monkeypatch):
    session = Mock(); session.get.return_value = response(b"<html>Login</html>")
    monkeypatch.setattr(single, "make_session", Mock(return_value=session))
    monkeypatch.setattr(sys, "argv", ["download_paper.py", "--url", "https://example.test/a.pdf",
        "--output", str(tmp_path)])
    with pytest.raises(SystemExit) as result:
        single.main()
    assert result.value.code == 1
    assert not list(tmp_path.glob("*.pdf"))


def test_single_cli_success_saves_readable_pdf(tmp_path, monkeypatch):
    body = pdf_bytes()
    session = Mock(); session.get.return_value = response(body)
    monkeypatch.setattr(single, "make_session", Mock(return_value=session))
    monkeypatch.setattr(sys, "argv", ["download_paper.py", "--url", "https://example.test/a.pdf",
        "--output", str(tmp_path)])
    with pytest.raises(SystemExit) as result:
        single.main()
    assert result.value.code == 0
    files = list(tmp_path.glob("*.pdf"))
    assert len(files) == 1 and files[0].read_bytes() == body


def test_cookie_merge_overrides_duplicates_and_does_not_mutate_input():
    global_jar = synthetic_cookie_jar()
    domain_jar = synthetic_cookie_jar()
    domain_cookie = next(iter(domain_jar))
    domain_cookie.value = "configured-test-value"
    merged = dl.merge_cookie_jars(global_jar, {"ieeexplore.ieee.org": domain_jar})
    cookie = next(iter(merged))
    assert cookie.value == "configured-test-value"
    cookie.value = "changed-test-value"
    assert next(iter(global_jar)).value == "not-a-real-secret"
    assert next(iter(domain_jar)).value == "configured-test-value"


def test_single_session_drops_domainless_cookies(monkeypatch):
    jar = MozillaCookieJar()
    jar.set_cookie(requests.cookies.create_cookie("unsafe", "synthetic-value"))
    monkeypatch.setattr(single, "load_cookies_txt", Mock(return_value=jar))
    with single.make_session("OfflineTest", Path("synthetic.json")) as session:
        prepared = session.prepare_request(requests.Request("GET", "https://unrelated.test/"))
    assert "Cookie" not in prepared.headers


def test_batch_does_not_report_corrupt_pdf_as_downloaded(tmp_path, monkeypatch):
    session = Mock(); session.get.return_value = response(b"%PDF-1.7\ntruncated")
    monkeypatch.setattr(dl, "make_session", Mock(return_value=session))
    item = ReferenceItem(1, "Paper", urls=["https://example.test/a.pdf"])
    kw = phase_kwargs(dl.run_initial_download_phase, [item], tmp_path)
    dl.run_initial_download_phase(**kw)
    assert item.download_status != "downloaded_pdf"
    assert any(row.outcome == "invalid_pdf" for row in kw["logger"]._rows)
    assert not list(tmp_path.glob("*.pdf"))


def test_batch_preserves_old_pdf_and_original_reference_number(tmp_path, monkeypatch):
    old, new = pdf_bytes("Old"), pdf_bytes("New")
    (tmp_path / "007.pdf").write_bytes(old)
    session = Mock(); session.get.return_value = response(new)
    monkeypatch.setattr(dl, "make_session", Mock(return_value=session))
    item = ReferenceItem(7, "New paper", urls=["https://example.test/a.pdf"])
    dl.run_initial_download_phase(**phase_kwargs(dl.run_initial_download_phase, [item], tmp_path))
    assert item.download_status == "downloaded_pdf"
    assert item.downloaded_file.startswith("007")
    assert item.downloaded_file != "007.pdf"
    assert (tmp_path / "007.pdf").read_bytes() == old
    assert (tmp_path / item.downloaded_file).read_bytes() == new
