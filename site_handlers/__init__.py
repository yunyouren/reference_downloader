from __future__ import annotations

from typing import Iterable

import requests

from .registry import dispatch
from .registry import HandlerResult

from . import ieee as _ieee
from . import springer as _springer


def dispatch_html(
    *,
    host: str,
    session: requests.Session,
    item,
    helpers,
    downloads_dir,
    mismatch_dir,
    verified_dir,
    timeout: int,
    attempt: int,
    verify_title_rename: bool,
    verify_title_threshold: float,
    verify_rename_mode: str,
    verify_weights,
    logger,
    phase: str,
    seen: set[str],
    prefix: str,
    final_url: str,
    first_chunk: bytes,
    chunks: Iterable[bytes],
) -> HandlerResult:
    handler = dispatch(host)
    if handler is None:
        return "unhandled"
    return handler(
        session=session,
        item=item,
        helpers=helpers,
        downloads_dir=downloads_dir,
        mismatch_dir=mismatch_dir,
        verified_dir=verified_dir,
        timeout=timeout,
        attempt=attempt,
        verify_title_rename=verify_title_rename,
        verify_title_threshold=verify_title_threshold,
        verify_rename_mode=verify_rename_mode,
        verify_weights=verify_weights,
        logger=logger,
        phase=phase,
        seen=seen,
        prefix=prefix,
        final_url=final_url,
        first_chunk=first_chunk,
        chunks=chunks,
    )
