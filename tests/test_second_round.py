"""Offline acceptance checks for verification, staged lookup and run control."""
import io
import json
import queue
import threading
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from pypdf import PdfWriter

from core.verify import verify_and_rename_pdf
from src.models import SecondaryLookupCache
from src.models import ReferenceItem, PipelineConfig, DomainLimiter
from src import downloader as dl
from core.runtime import RunControl, RunCancelled, LimitedAdapter
from core.runtime import ControlledSession
import requests
from test_download_reliability import phase_kwargs, mock_secondary_sources, pdf_bytes, response


BODY = "1. Introduction\n" + "This paper presents experimental results for power conversion and validates the proposed design. " * 8


class Page:
    def __init__(self, text): self.text = text
    def extract_text(self): return self.text


def decision(tmp_path, text, title="Wrong metadata", expected="High Voltage Modular Converter", **extra):
    path = tmp_path / "001.pdf"
    path.write_bytes(b"synthetic fixture using injected reader")
    (tmp_path / "mismatch").mkdir(exist_ok=True)
    factory = Mock(return_value=type("Reader", (), {"metadata": {"/Title": title}, "pages": [Page(text)]})())
    result = verify_and_rename_pdf(prefix="001", out_file=path, downloads_dir=tmp_path,
        verified_dir=None, mismatch_dir=tmp_path / "mismatch", expected_title=expected,
        ref_year=extra.pop("ref_year", None), surname=extra.pop("surname", ""), verify_title_threshold=.65, verify_weights=None,
        reader_cls=factory, **extra)
    return result, factory


def test_multiline_title_and_single_reader(tmp_path):
    result, factory = decision(tmp_path, "High Voltage\nModular Converter\n" + BODY)
    assert result.outcome == "downloaded_pdf"
    assert factory.call_count == 1


def test_matching_metadata_on_blank_page_is_not_fulltext(tmp_path):
    result, _ = decision(tmp_path, "", title="High Voltage Modular Converter")
    assert result.outcome != "downloaded_pdf"


@pytest.mark.parametrize("marker", ["Supporting Information", "Supplementary Material", "This is a preview of the article", "Abstract only"])
def test_non_main_document_is_not_accepted(tmp_path, marker):
    result, _ = decision(tmp_path, marker + "\nHigh Voltage Modular Converter\n" + BODY,
                         title="High Voltage Modular Converter")
    assert result.outcome != "downloaded_pdf"


def test_primary_doi_can_resolve_fragmented_title(tmp_path):
    result, _ = decision(tmp_path, "A fragmented heading\ndoi:10.1234/primary\n" + BODY,
        expected_dois=["10.1234/primary"])
    assert result.outcome == "downloaded_pdf"


def test_doi_in_references_does_not_prove_identity(tmp_path):
    result, _ = decision(tmp_path, "Unrelated paper\n" + BODY + "\nReferences\n[1] DOI: 10.1234/primary",
        expected_dois=["10.1234/primary"])
    assert result.outcome != "downloaded_pdf"


def test_negative_cache_and_expiry(tmp_path):
    now = [1000.0]
    cache = SecondaryLookupCache(tmp_path / "cache.json", positive_ttl=100, negative_ttl=10,
                                 clock=lambda: now[0])
    cache.set("miss", [], [])
    assert cache.get("miss") == ([], [])
    now[0] += 11
    assert cache.get("miss") is None
    cache.set("found", ["10.1234/a"], [])
    now[0] += 50
    assert cache.get("found") == (["10.1234/a"], [])
    now[0] += 51
    assert cache.get("found") is None


def test_early_download_stops_remaining_sources(tmp_path, monkeypatch):
    ranked = Mock(return_value=([], ["https://example.test/good.pdf"]))
    mock_secondary_sources(monkeypatch, ranked)
    def accept(**kw):
        kw["item"].download_status = "downloaded_pdf"
        kw["item"].downloaded_file = "001.pdf"
    monkeypatch.setattr(dl, "try_download", accept)
    item = ReferenceItem(1, "Power converter paper", download_status="failed")
    dl.enrich_failed_references(**phase_kwargs(dl.enrich_failed_references, [item], tmp_path))
    assert item.download_status == "downloaded_pdf"
    dl.lookup_openalex_pdf_urls_by_title.assert_not_called()
    dl.lookup_semanticscholar_pdf_urls_by_title.assert_not_called()


def test_unknown_document_does_not_stop_fallback_search(tmp_path, monkeypatch):
    mock_secondary_sources(monkeypatch, Mock(return_value=([], ["https://example.test/scan.pdf"])))
    dl.lookup_openalex_pdf_urls_by_title.return_value = ["https://example.test/full.pdf"]
    attempted = []
    def attempt(**kw):
        item = kw["item"]; attempted.extend(item.urls)
        item.download_status = "downloaded_pdf" if "full.pdf" in item.urls[0] else "needs_review"
        item.downloaded_file = "001.pdf"
    monkeypatch.setattr(dl, "try_download", attempt)
    item = ReferenceItem(1, "Power converter paper", download_status="failed")
    dl.enrich_failed_references(**phase_kwargs(dl.enrich_failed_references, [item], tmp_path))
    assert item.download_status == "downloaded_pdf"
    assert len(attempted) == 2 and len(set(attempted)) == 2
    dl.lookup_semanticscholar_pdf_urls_by_title.assert_not_called()


def test_power_engineering_skips_irrelevant_sources(tmp_path, monkeypatch):
    mock_secondary_sources(monkeypatch)
    item = ReferenceItem(1, "A solid state transformer for power converters", download_status="failed")
    dl.enrich_failed_references(**phase_kwargs(dl.enrich_failed_references, [item], tmp_path))
    for source in [dl.lookup_europepmc_pdf_urls_by_title, dl.lookup_biorxiv_pdf_urls_by_title,
                   dl.lookup_google_books_pdf_urls, dl.lookup_ssrn_pdf_urls_by_title, dl.lookup_chemrxiv_pdf_urls_by_title]:
        source.assert_not_called()


def test_negative_source_cache_prevents_repeat_queries(tmp_path, monkeypatch):
    ranked = Mock(return_value=([], [])); mock_secondary_sources(monkeypatch, ranked)
    cache = SecondaryLookupCache(tmp_path / "cache.json")
    for _ in range(2):
        item = ReferenceItem(1, "Power converter paper", download_status="failed")
        kw = phase_kwargs(dl.enrich_failed_references, [item], tmp_path); kw["secondary_cache"] = cache
        dl.enrich_failed_references(**kw)
    assert ranked.call_count == 1
    assert dl.lookup_openalex_pdf_urls_by_title.call_count == 1


def test_transient_lookup_failure_is_not_cached_as_missing(tmp_path, monkeypatch):
    ranked = Mock(return_value=([], [])); mock_secondary_sources(monkeypatch, ranked)
    session = Mock(); session.transient_errors = 0
    def transient(*a, **k):
        session.transient_errors += 1
        return [], []
    ranked.side_effect = transient
    monkeypatch.setattr(dl, "make_session", Mock(return_value=session))
    cache = SecondaryLookupCache(tmp_path / "cache.json")
    for _ in range(2):
        item = ReferenceItem(1, "Power converter paper", download_status="failed")
        kw = phase_kwargs(dl.enrich_failed_references, [item], tmp_path); kw["secondary_cache"] = cache
        dl.enrich_failed_references(**kw)
    assert ranked.call_count == 2
    assert dl.lookup_openalex_pdf_urls_by_title.call_count == 1


def test_stop_prevents_submission_and_saves_finished_item(tmp_path, monkeypatch):
    stop = threading.Event(); calls = []
    def accept(**kw):
        item = kw["item"]; calls.append(item.number)
        path = kw["downloads_dir"] / "001.pdf"; path.write_bytes(pdf_bytes())
        item.download_status, item.downloaded_file = "downloaded_pdf", path.name
        item.verification_version = 2
        stop.set()
    monkeypatch.setattr(dl, "try_download", accept)
    items = [ReferenceItem(i, f"Paper {i}") for i in range(1, 6)]
    cfg = PipelineConfig(input_pdf=tmp_path / "unused.pdf", output_dir=tmp_path,
                         workers=1, interactive="false", domain_cookies_config={}, secondary_lookup=True)
    messages = []
    dl.run_pipeline_from_refs(items, cfg, cancel_event=stop, progress_callback=messages.append)
    rows = json.loads((tmp_path / "references.json").read_text(encoding="utf-8"))
    assert calls == [1]
    assert rows[0]["download_status"] == "downloaded_pdf"
    assert all(r["download_status"] == "not_attempted" for r in rows[1:])
    assert any(m.startswith("Stopped") for m in messages)


def test_checkpoint_visible_before_other_worker_finishes(tmp_path, monkeypatch):
    first_done = threading.Event(); release = threading.Event(); stop = threading.Event()
    errors = []
    def download(**kw):
        item = kw["item"]
        if item.number == 1:
            path = kw["downloads_dir"] / "001.pdf"; path.write_bytes(pdf_bytes())
            item.download_status, item.downloaded_file = "downloaded_pdf", path.name
        else:
            item.text = "in-flight mutation must not enter the checkpoint"
            assert release.wait(3)
            kw["control"].check()
    monkeypatch.setattr(dl, "try_download", download)
    cfg = PipelineConfig(input_pdf=tmp_path / "unused.pdf", output_dir=tmp_path,
        workers=2, interactive="false", domain_cookies_config={})
    items = [ReferenceItem(1, "Paper 1"), ReferenceItem(2, "Paper 2")]
    def progress(message):
        if message.startswith("initial: [1]"): first_done.set()
    def run():
        try: dl.run_pipeline_from_refs(items, cfg, cancel_event=stop, progress_callback=progress)
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=run); thread.start()
    try:
        assert first_done.wait(3)
        rows = json.loads((tmp_path / "references.json").read_text(encoding="utf-8"))
        assert rows[0]["download_status"] == "downloaded_pdf"
        assert rows[1]["text"] == "Paper 2"
        assert thread.is_alive()
    finally:
        stop.set(); release.set(); thread.join(3)
    assert not thread.is_alive() and not errors


def test_cancel_interrupts_long_backoff():
    stop = threading.Event(); stop.set()
    with pytest.raises(RunCancelled): RunControl(stop).wait(120)


def test_cancel_while_waiting_for_domain_slot():
    limiter = DomainLimiter(1, 0); held = limiter.acquire("example.test")
    stop = threading.Event(); errors = []
    def blocked():
        try: limiter.acquire("example.test", cancel_event=stop)
        except RunCancelled: errors.append("cancelled")
    thread = threading.Thread(target=blocked); thread.start(); stop.set(); thread.join(2)
    limiter.release(held)
    assert not thread.is_alive() and errors == ["cancelled"]


def test_gui_progress_arrives_before_task_finishes(monkeypatch, tmp_path):
    import reference_tool_gui as gui
    instance = gui.ReferenceToolGUI.__new__(gui.ReferenceToolGUI)
    instance.log_queue = queue.Queue(); instance._stop_requested = threading.Event()
    entered = threading.Event(); release = threading.Event(); original_stdout = sys.stdout
    def run(cfg, *, cancel_event, progress_callback):
        assert sys.stdout is original_stdout
        progress_callback("live progress\n"); entered.set()
        assert release.wait(3)
        assert cancel_event is instance._stop_requested
    monkeypatch.setattr(gui, "run_pipeline", run)
    thread = threading.Thread(target=instance._pipeline_runner, args=({"input":"unused.pdf", "output":str(tmp_path)},))
    thread.start()
    try:
        assert entered.wait(3)
        assert instance.log_queue.get(timeout=1) == "live progress\n"
        assert thread.is_alive()
        instance._stop_requested.set()
    finally:
        release.set(); thread.join(3)
    assert instance.log_queue.get(timeout=1) == "__EXIT__:130"


def test_gui_stop_sets_shared_event_and_stopping_status():
    from reference_tool_gui import ReferenceToolGUI
    instance = ReferenceToolGUI.__new__(ReferenceToolGUI)
    instance._pipeline_thread = Mock(); instance._pipeline_thread.is_alive.return_value = True
    instance._stop_requested = threading.Event(); instance.stop_btn = Mock(); instance._set_status = Mock()
    instance._stop()
    assert instance._stop_requested.is_set()
    instance._set_status.assert_called_once_with("stopping")


@pytest.mark.parametrize("_round", range(5))
def test_concurrent_verified_rename_never_overwrites(tmp_path, _round):
    def run(number):
        path = tmp_path / f"{number:03d}.pdf"; body = f"fixture-{number}".encode(); path.write_bytes(body)
        reader = lambda _: type("Reader", (), {"metadata": {"/Title":"High Voltage Modular Converter"},
            "pages":[Page("High Voltage Modular Converter\n" + BODY)]})()
        result = verify_and_rename_pdf(prefix=f"{number:03d}",out_file=path,downloads_dir=tmp_path,
            verified_dir=tmp_path/"verified",mismatch_dir=None,expected_title="High Voltage Modular Converter",
            ref_year=None,surname="",verify_title_threshold=.65,verify_weights=None,
            verify_rename_mode="original",reader_cls=reader)
        return result.file_path, body
    with ThreadPoolExecutor(max_workers=6) as pool: results = list(pool.map(run,range(1,13)))
    assert len({p for p,_ in results}) == 12
    assert all(p.read_bytes()==body for p,body in results)


def test_standalone_hyphen_title_lines(tmp_path):
    result, _ = decision(tmp_path, "99+% Efficient Ultra\n-\nCompact Solid\n-\nState\nTransformers\n"+BODY,
        expected="99+% efficient ultracompact solid-state transformers")
    assert result.outcome == "downloaded_pdf"


def test_small_spelling_error_needs_independent_author_year(tmp_path):
    result, _ = decision(tmp_path, "The Essence of Solid-State Transformers\nKolar 2023\n"+BODY,
        title="The Essence of Solid-State Transformers",expected="The essence of solidsate transformers",
        ref_year=2023,surname="kolar")
    assert result.outcome == "downloaded_pdf"
    assert result.identity_match == "title_spelling_with_author_year"


def test_spelling_tolerance_does_not_change_voltage_rating(tmp_path):
    result, _ = decision(tmp_path, "12 kV High Voltage Modular Converter\nKolar 2023\n"+BODY,
        title="12 kV High Voltage Modular Converter",expected="13 kV High Voltage Modular Converter",
        ref_year=2023,surname="kolar",expected_dois=["10.1234/wanted"])
    # Without an authoritative DOI, a changed number must not be excused as a typo.
    assert result.identity_match != "title_spelling_with_author_year"
    assert result.outcome != "downloaded_pdf"


def test_conflicting_doi_is_not_accepted_even_with_matching_title(tmp_path):
    result, _ = decision(tmp_path, "High Voltage Modular Converter\ndoi:10.1234/wrong\n"+BODY,
        title="High Voltage Modular Converter",expected_dois=["10.1234/wanted"])
    assert result.outcome == "needs_review" and result.reason == "identifier_conflict"


def test_inline_citation_doi_is_not_primary_identity(tmp_path):
    result, _ = decision(tmp_path, "Unrelated title\n"+BODY+"\nAnother paper uses doi:10.1234/wanted.",
        expected_dois=["10.1234/wanted"])
    assert result.outcome != "downloaded_pdf"


def test_primary_patent_id_and_zero_padding(tmp_path):
    result, _ = decision(tmp_path,"US010439407B2\nUnusual extracted title\n"+BODY,
        title="US00000010439407B220191008",expected_identifiers=["US10439407B2"])
    assert result.outcome == "downloaded_pdf" and result.identity_match == "identifier"


def test_cited_patent_number_does_not_establish_identity(tmp_path):
    result, _ = decision(tmp_path, "US12345678B2\nOther patent\nReferences Cited US10439407B2\n"+BODY,
        expected_identifiers=["US10439407B2"])
    assert result.outcome != "downloaded_pdf"


def test_article_mentioning_supplementary_information_is_not_si(tmp_path):
    result, _ = decision(tmp_path, "High Voltage Modular Converter\n"+BODY+"\nSupporting Information describes additional results.")
    assert result.outcome == "downloaded_pdf"


class RawFixture:
    def __init__(self, content=b"offline body"):
        self.content = content; self.closed = False
    def stream(self, *a, **k): yield self.content
    def close(self): self.closed = True
    def release_conn(self): pass


def raw_response(content=b"offline body", status=200, headers=None):
    result = requests.Response(); result.status_code=status
    result.headers.update(headers or {}); result.raw=RawFixture(content)
    return result


def test_domain_slot_is_held_until_body_or_close(monkeypatch):
    limiter=DomainLimiter(1,0); control=RunControl()
    monkeypatch.setattr(requests.adapters.HTTPAdapter,"send",lambda *a,**k:raw_response())
    adapter=LimitedAdapter(control=control,domain_limiter=limiter)
    request=requests.Request("GET","https://example.test/a").prepare()
    result=adapter.send(request)
    sem=limiter._semaphores["example.test"]
    assert not sem.acquire(blocking=False)
    assert result.content==b"offline body"
    assert sem.acquire(blocking=False); sem.release()
    result.close()
    assert sem._value==1


def test_adapter_close_releases_unconsumed_body(monkeypatch):
    limiter=DomainLimiter(1,0)
    monkeypatch.setattr(requests.adapters.HTTPAdapter,"send",lambda *a,**k:raw_response())
    adapter=LimitedAdapter(control=RunControl(),domain_limiter=limiter)
    result=adapter.send(requests.Request("GET","https://example.test/a").prepare())
    result.close()
    assert limiter._semaphores["example.test"]._value==1


def test_actual_redirect_hosts_each_use_their_limiter(monkeypatch):
    domain=Mock();domain.acquire.return_value=None
    api=Mock();api.acquire.return_value=None
    calls=[]
    def send(self,request,**kwargs):
        calls.append(request.url)
        r=raw_response(b"",302,{"Location":"https://api.crossref.org/works"}) if len(calls)==1 else raw_response(b"{}")
        r.url=request.url; r.request=request
        return r
    monkeypatch.setattr(requests.adapters.HTTPAdapter,"send",send)
    session=dl.make_session(4,"OfflineTest",None,control=RunControl(),domain_limiter=domain,api_limiter=api)
    with session:
        result=session.get("https://doi.org/10.1234/test")
    assert result.status_code==200 and len(calls)==2
    domain.acquire.assert_called_once(); api.acquire.assert_called_once()
    assert domain.acquire.call_args.args[0]=="doi.org"
    assert api.acquire.call_args.args[0]=="api.crossref.org"


def test_adapter_retry_after_blocks_host_without_negative_cache(monkeypatch):
    limiter=DomainLimiter(1,0)
    monkeypatch.setattr(requests.adapters.HTTPAdapter,"send",lambda *a,**k:raw_response(b"",429,{"Retry-After":"30"}))
    adapter=LimitedAdapter(control=RunControl(),domain_limiter=limiter)
    result=adapter.send(requests.Request("GET","https://example.test/a").prepare()); result.close()
    assert adapter.transient_errors==1
    assert limiter.compute_wait_seconds("example.test")>25


def test_bad_api_json_counts_as_transient_not_missing(monkeypatch):
    def send(self,request,**kwargs):
        result=raw_response(b"not JSON");result.url=request.url;result.request=request
        return result
    monkeypatch.setattr(requests.adapters.HTTPAdapter,"send",send)
    with dl.make_session(4,"OfflineTest",None,control=RunControl(),domain_limiter=DomainLimiter(1,0)) as session:
        result=session.get("https://example.test/api")
        with pytest.raises(ValueError): result.json()
        assert session.transient_errors>0


def test_old_blank_pdf_is_reclassified_not_silently_resumed(tmp_path,monkeypatch):
    writer=PdfWriter();writer.add_blank_page(width=600,height=800)
    path=tmp_path/"001.pdf"
    with path.open("wb") as stream:writer.write(stream)
    item=ReferenceItem(1,"A paper",download_status="downloaded_pdf",downloaded_file=path.name)
    dl.run_initial_download_phase(**phase_kwargs(dl.run_initial_download_phase,[item],tmp_path))
    assert item.download_status=="needs_review"
    assert path.exists()


def test_enabling_identity_checks_revalidates_prior_unchecked_download(tmp_path,monkeypatch):
    path=tmp_path/"001.pdf";path.write_bytes(pdf_bytes("Unrelated extracted title"))
    item=ReferenceItem(1,'Author, "Completely different subject", 2020',download_status="downloaded_pdf",
        downloaded_file=path.name,verification_version=2,identity_checked=False)
    session=Mock();session.get.side_effect=AssertionError("no network candidates expected")
    monkeypatch.setattr(dl,"make_session",Mock(return_value=session))
    kw=phase_kwargs(dl.run_initial_download_phase,[item],tmp_path);kw["verify_title_rename"]=True
    dl.run_initial_download_phase(**kw)
    assert item.download_status!="downloaded_pdf" and item.identity_checked
    assert path.exists()


def test_batch_html_handler_releases_parent_slot_at_concurrency_one(tmp_path,monkeypatch):
    from src.models import DownloadLogger
    body=pdf_bytes("High Voltage Modular Converter")
    sent=[]
    def send(self,request,**kwargs):
        sent.append(request.url)
        if len(sent)==1:
            result=raw_response(b'<meta name="citation_pdf_url" content="https://link.springer.com/new.pdf">')
            result.headers["Content-Type"]="text/html"
        else:
            result=raw_response(body);result.headers["Content-Type"]="application/pdf"
        result.url=request.url;result.request=request
        return result
    monkeypatch.setattr(requests.adapters.HTTPAdapter,"send",send)
    control=RunControl();limiter=DomainLimiter(1,0)
    item=ReferenceItem(1,'Author, "High Voltage Modular Converter"',urls=["https://link.springer.com/article/1"])
    with dl.make_session(4,"OfflineTest",None,control=control,domain_limiter=limiter) as session:
        dl.try_download(session=session,item=item,downloads_dir=tmp_path,meta_dir=None,landing_dir=None,
            mismatch_dir=None,timeout=1,retries=1,use_doi=False,max_candidates_per_item=2,
            domain_limiter=limiter,logger=DownloadLogger(),phase="initial",verify_title_rename=True,
            verify_title_threshold=.65,verify_rename_mode="number_and_original",verify_weights=None,
            verified_dir=None,control=control)
    assert item.download_status=="downloaded_pdf" and len(sent)==2
    assert limiter._semaphores["link.springer.com"]._value==1


def test_single_entry_does_not_accept_blank_as_main_text(tmp_path):
    import download_paper
    writer=PdfWriter();writer.add_blank_page(width=600,height=800);stream=io.BytesIO();writer.write(stream)
    session=Mock();session.get.return_value=response(stream.getvalue())
    result=download_paper.download_paper(session,["https://example.test/a.pdf"],tmp_path,1)
    assert result is None
    assert not list(tmp_path.glob("*.pdf"))
    assert len(list((tmp_path/"review_pdfs").glob("*.pdf")))==1


def test_later_body_citation_title_cannot_establish_identity(tmp_path):
    path=tmp_path/"001.pdf";path.write_bytes(b"injected PDF reader fixture")
    reader=lambda _:type("Reader",(),{"metadata":{"/Title":"An unrelated original document"},
        "pages":[Page("An unrelated original document\n"+BODY),Page("High Voltage Modular Converter\n"+BODY)]})()
    result=verify_and_rename_pdf(prefix="001",out_file=path,downloads_dir=tmp_path,
        verified_dir=None,mismatch_dir=None,expected_title="High Voltage Modular Converter",
        ref_year=None,surname="",verify_title_threshold=.65,verify_weights=None,reader_cls=reader)
    assert result.outcome!="downloaded_pdf"


def test_section_number_is_not_confused_with_voltage_rating(tmp_path):
    result,_=decision(tmp_path,"I.1.1 High Voltage 400-kW Modular Converter\n"+BODY,
        expected="High Voltage 400-kW Modular Converter")
    assert result.outcome=="downloaded_pdf"


def test_loopback_http_redirect_html_pdf_with_one_domain_slot(tmp_path,monkeypatch):
    """Actual requests/urllib3 I/O, loopback only, no publisher or real credentials."""
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    from site_handlers.springer import handle_springer_html
    from src.models import DownloadLogger
    payload=pdf_bytes("High Voltage Modular Converter")
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            seen.append(self.path)
            if self.path=="/redirect":
                self.send_response(302);self.send_header("Location","/article")
                self.send_header("Content-Length","0");self.end_headers();return
            if self.path=="/article":
                body=f'<meta name="citation_pdf_url" content="http://127.0.0.1:{self.server.server_port}/new.pdf">'.encode()
                mime="text/html"
            else:
                body=payload;mime="application/pdf"
            self.send_response(200);self.send_header("Content-Type",mime)
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def dispatch(**kwargs):
        kwargs.pop("host")
        return handle_springer_html(**kwargs)
    monkeypatch.setattr(dl.site_handlers,"dispatch_html",dispatch)
    stop=threading.Event();timer=threading.Timer(3,stop.set);timer.start()
    control=RunControl(stop);limiter=DomainLimiter(1,0)
    try:
        url=f"http://127.0.0.1:{server.server_port}/redirect"
        item=ReferenceItem(1,'Author, "High Voltage Modular Converter"',urls=[url])
        with dl.make_session(4,"OfflineTest",None,control=control,domain_limiter=limiter) as session:
            session.trust_env=False
            dl.try_download(session=session,item=item,downloads_dir=tmp_path,meta_dir=None,
                landing_dir=None,mismatch_dir=None,timeout=2,retries=1,use_doi=False,
                max_candidates_per_item=2,domain_limiter=limiter,logger=DownloadLogger(),
                phase="initial",verify_title_rename=True,verify_title_threshold=.65,
                verify_rename_mode="number_and_original",verify_weights=None,verified_dir=None,control=control)
        assert item.download_status=="downloaded_pdf"
        assert (tmp_path/item.downloaded_file).read_bytes()==payload
        assert seen==["/redirect","/article","/new.pdf"]
        assert limiter._semaphores["127.0.0.1"]._value==1
    finally:
        timer.cancel();server.shutdown();server.server_close();thread.join(2)
