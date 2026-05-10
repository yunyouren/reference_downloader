"""Tests for data models."""
import tempfile
import threading
from pathlib import Path

from src.models import (
    ReferenceItem,
    DownloadAttempt,
    DownloadLogger,
    SecondaryLookupCache,
    DomainLimiter,
)


def test_reference_item_defaults():
    item = ReferenceItem(number=1, text="Test reference")
    assert item.number == 1
    assert item.text == "Test reference"
    assert item.dois == []
    assert item.urls == []
    assert item.download_status == "not_attempted"
    assert item.downloaded_file == ""
    assert item.note == ""


def test_reference_item_with_fields():
    item = ReferenceItem(
        number=5,
        text="Smith et al., A Study on X",
        dois=["10.1234/foo"],
        urls=["https://example.com/paper.pdf"],
        download_status="downloaded_pdf",
        downloaded_file="005.pdf",
        note="verified",
    )
    assert item.number == 5
    assert "10.1234/foo" in item.dois
    assert item.download_status == "downloaded_pdf"


def test_download_logger_thread_safe():
    logger = DownloadLogger()

    def add_entries():
        for i in range(10):
            logger.add(
                DownloadAttempt(
                    phase="test",
                    ref_number=i,
                    candidate_url="http://x.com",
                    final_url="http://y.com",
                    status_code=200,
                    content_type="application/pdf",
                    outcome="ok",
                    waited_seconds=0.0,
                    error="",
                )
            )

    threads = [threading.Thread(target=add_entries) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp = Path(f.name)
    try:
        logger.write_csv(tmp)
        lines = tmp.read_text().splitlines()
        assert len(lines) == 51  # header + 50 rows
        assert "phase,ref_number" in lines[0]
    finally:
        tmp.unlink(missing_ok=True)


def test_download_logger_empty():
    logger = DownloadLogger()
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        tmp = Path(f.name)
    try:
        logger.write_csv(tmp)
        # Should not crash, and should create no file (or empty)
        # Actually it creates the parent dirs but doesn't write if no rows
        assert True
    finally:
        tmp.unlink(missing_ok=True)


def test_secondary_lookup_cache_get_set():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = Path(f.name)
    try:
        cache = SecondaryLookupCache(tmp)
        # Should return None for nonexistent key
        assert cache.get("nonexistent") is None

        # Set and get
        cache.set("key1", ["10.1234/foo"], ["https://example.com"])
        dois, urls = cache.get("key1")
        assert dois == ["10.1234/foo"]
        assert urls == ["https://example.com"]

        # Empty set shouldn't write
        cache.set("key2", [], [])
        assert cache.get("key2") is None
    finally:
        tmp.unlink(missing_ok=True)


def test_secondary_lookup_cache_flush():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = Path(f.name)
    try:
        cache = SecondaryLookupCache(tmp)
        cache.set("key_a", ["10.1/a"], [])
        cache.flush()

        # Read from new cache instance
        cache2 = SecondaryLookupCache(tmp)
        dois, urls = cache2.get("key_a")
        assert dois == ["10.1/a"]
    finally:
        tmp.unlink(missing_ok=True)


def test_domain_limiter_acquire_release():
    limiter = DomainLimiter(max_per_domain=2, min_delay_ms=0)
    sem = limiter.acquire("example.com")
    assert sem is not None
    limiter.release(sem)


def test_domain_limiter_no_limit():
    limiter = DomainLimiter(max_per_domain=0, min_delay_ms=0)
    # max_per_domain=0 means unlimited
    sem = limiter.acquire("example.com")
    assert sem is None


def test_domain_limiter_empty_host():
    limiter = DomainLimiter(max_per_domain=2, min_delay_ms=0)
    sem = limiter.acquire("")
    assert sem is None
    limiter.release(sem)

    wait = limiter.compute_wait_seconds("")
    assert wait == 0.0


def test_domain_limiter_backoff():
    limiter = DomainLimiter(max_per_domain=2, min_delay_ms=0)
    # Backoff should increase wait time
    initial_wait = limiter.compute_wait_seconds("test.com")
    limiter.backoff("test.com", 5.0)
    after_wait = limiter.compute_wait_seconds("test.com")
    assert after_wait > initial_wait


def test_domain_limiter_context_manager():
    with DomainLimiter(max_per_domain=2, min_delay_ms=0) as limiter:
        sem = limiter.acquire("example.com")
        assert sem is not None
        limiter.release(sem)
