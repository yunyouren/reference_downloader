from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlparse


_DEBUG = os.environ.get("REFERENCE_DEBUG") or os.environ.get("DEBUG")


def normalize_candidate_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""

    try:
        p = urlparse(raw)
        host = (p.hostname or "").lower()

        if host == "xplorestaging.ieee.org":
            return p._replace(scheme="https", netloc="ieeexplore.ieee.org").geturl()

        if host == "api.elsevier.com":
            m = re.search(r"/PII:([^/?#]+)", p.path, flags=re.IGNORECASE)
            if m:
                pii = m.group(1).strip()
                if pii:
                    return f"https://linkinghub.elsevier.com/retrieve/pii/{pii}"
            m2 = re.search(r"/pii/([^/?#]+)", p.path, flags=re.IGNORECASE)
            if m2:
                pii = m2.group(1).strip()
                if pii:
                    return f"https://linkinghub.elsevier.com/retrieve/pii/{pii}"
            return ""
    except Exception:
        if _DEBUG:
            print(f"[DEBUG urls] normalize_candidate_url failed on {url!r}", file=sys.stderr)
        return raw

    return raw
