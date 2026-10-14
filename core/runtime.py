"""Cooperative stop, per-request limits (including redirects), progress callbacks."""
from __future__ import annotations

import threading
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from core.http import parse_retry_after_seconds


class RunCancelled(Exception):
    pass


class RunControl:
    def __init__(self, cancel_event=None, progress_callback=None):
        self.event = cancel_event or threading.Event()
        self.callback = progress_callback

    def check(self):
        if self.event.is_set():
            raise RunCancelled()

    def wait(self, seconds):
        if self.event.wait(max(0.0, seconds)):
            raise RunCancelled()

    def emit(self, message):
        if self.callback is not None:
            self.callback(message.rstrip("\n") + "\n")
        else:
            print(message)


class LimitedRaw:
    """Keep the domain slot until body consumption or close, releasing once."""
    def __init__(self, raw, release, control):
        self.raw, self.release, self.control = raw, release, control

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def stream(self, *args, **kwargs):
        try:
            for chunk in self.raw.stream(*args, **kwargs):
                self.control.check()
                yield chunk
        except BaseException:
            self.raw.close()
            raise
        finally:
            self.release()

    def read(self, *args, **kwargs):
        try:
            self.control.check()
            result = self.raw.read(*args, **kwargs)
            if not result or (not args and kwargs.get("amt") is None):
                self.release()
            return result
        except BaseException:
            self.raw.close()
            self.release()
            raise

    def close(self):
        try:
            return self.raw.close()
        finally:
            self.release()

    def release_conn(self):
        try:
            return self.raw.release_conn()
        finally:
            self.release()


class LimitedAdapter(HTTPAdapter):
    def __init__(self, *, control, domain_limiter, api_limiter=None, **kwargs):
        self.control = control
        self.domain_limiter = domain_limiter
        self.api_limiter = api_limiter
        self.transient_errors = 0
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        self.control.check()
        host = (urlparse(request.url).hostname or "").lower()
        api = host.startswith("api.") or host in {"export.arxiv.org", "www.ebi.ac.uk", "eutils.ncbi.nlm.nih.gov", "www.googleapis.com"}
        limiter = self.api_limiter if api and self.api_limiter is not None else self.domain_limiter
        sem = limiter.acquire(host, cancel_event=self.control.event) if limiter is not None else None
        deferred = False
        release_lock = threading.Lock()
        released = False
        def release():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    if limiter is not None:
                        limiter.release(sem)
        try:
            response = super().send(request, **kwargs)
            if response.status_code in (401, 403, 408, 425, 429) or response.status_code >= 500:
                self.transient_errors += 1
            if response.status_code in (429, 503) and limiter is not None:
                delay = parse_retry_after_seconds(response.headers.get("retry-after", ""))
                limiter.backoff(host, delay if delay is not None else 5.0)
            if self.control.event.is_set():
                response.close()
                raise RunCancelled()
            if response.raw is not None and not response._content_consumed:
                response.raw = LimitedRaw(response.raw, release, self.control)
                deferred = True
            return response
        except requests.RequestException:
            self.transient_errors += 1
            raise
        finally:
            if not deferred:
                release()


class ControlledSession(requests.Session):
    def __init__(self, control):
        super().__init__()
        self.control = control
        self._local_errors = 0

    def request(self, *args, **kwargs):
        self.control.check()
        try:
            response = super().request(*args, **kwargs)
        except requests.RequestException:
            self._local_errors += 1
            raise
        original = response.iter_content
        original_json = response.json
        def parse_json(*a, **k):
            try:
                return original_json(*a, **k)
            except ValueError:
                self._local_errors += 1
                raise
        response.json = parse_json
        def chunks(*a, **k):
            for chunk in original(*a, **k):
                self.control.check()
                yield chunk
        response.iter_content = chunks
        if self.control.event.is_set():
            response.close()
            raise RunCancelled()
        return response

    @property
    def transient_errors(self):
        return self._local_errors + sum(getattr(adapter, "transient_errors", 0) for adapter in set(self.adapters.values()))
