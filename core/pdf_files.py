"""Shared structural PDF checks and non-overwriting, staged file publication.

Readability is not a claim of document identity or full-text completeness.
"""
from __future__ import annotations

import hashlib
import io
import tempfile
import os
import time
from pathlib import Path
from typing import Iterable

from core.http import is_probably_pdf

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    from PyPDF2 import PdfReader


def is_readable_pdf(source: Path | bytes) -> bool:
    try:
        if isinstance(source, bytes):
            if not is_probably_pdf(source):
                return False
            stream = io.BytesIO(source)
        else:
            stream = source.open("rb")
        with stream:
            header = stream.read(1024)
            if not is_probably_pdf(header):
                return False
            stream.seek(0)
            reader = PdfReader(stream)
            if reader.is_encrypted and not reader.decrypt(""):
                return False
            if not reader.pages:
                return False
            for page in reader.pages:
                # Resolve page objects, not just the advertised page count.
                _ = page.mediabox
            return True
    except Exception:
        return False


def contained_file(root: Path, name: str) -> Path | None:
    """Reject stale manifest paths that escape the download directory."""
    if not name:
        return None
    try:
        candidate = (root / name).resolve()
        candidate.relative_to(root.resolve())
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".state-", suffix=".tmp",
                                     mode="w", encoding="utf-8", newline="", delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(text)
        except BaseException:
            stream.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def _move_exclusive(source: Path, destination: Path) -> None:
    # Windows rename refuses existing destinations atomically. On POSIX use an
    # exclusive hard link on the same output volume, then remove the old name.
    if os.name == "nt":
        source.rename(destination)
    else:
        os.link(source, destination)
        try:
            source.unlink()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def move_without_overwrite(source: Path, destination: Path) -> Path:
    """Publish exclusively without a placeholder/replace race on Windows."""
    if source.resolve() == destination.resolve():
        return source
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        candidate = destination if suffix == 1 else destination.with_name(
            f"{destination.stem}_{suffix}{destination.suffix}"
        )
        for attempt in range(4):
            try:
                _move_exclusive(source, candidate)
                return candidate
            except FileExistsError:
                break
            except PermissionError as exc:
                if candidate.exists():
                    break
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 3:
                    raise
                time.sleep(.01 * (attempt + 1))
        suffix += 1


def save_pdf_stream(destination: Path, chunks: Iterable[bytes]) -> Path | None:
    """Validate a temporary file, then publish without overwriting any old file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".pdf-", suffix=".part", delete=False) as stream:
        staged = Path(stream.name)
        try:
            for chunk in chunks:
                if chunk:
                    stream.write(chunk)
        except BaseException:
            stream.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        if not is_readable_pdf(staged):
            return None
        return move_without_overwrite(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
