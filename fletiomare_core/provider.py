"""Provider client: Cloud Run service-to-service call (Google ID token) + the
member's opaque LISA token. Shared by every Fletiomare app that sits on top of
the lisa-auth provider.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)

_id_token_cache: Dict[str, Tuple[str, float]] = {}
_metadata_unavailable = False   # hard off-switch (tests / known off-GCE)
_metadata_retry_after = 0.0     # soft back-off after a transient probe failure


def id_token(audience: str) -> Optional[str]:
    """Google-signed ID token for `audience`, from the GCE metadata server.

    Returns None off-GCE (e.g. local dev): the call then carries no IAM token,
    which is fine against a public/local provider. Tokens are cached ~50 min; a
    failed probe backs off ~30s (not forever) so a transient cold-start blip
    self-heals instead of bricking the instance's auth.
    """
    global _metadata_retry_after
    if _metadata_unavailable:
        return None
    now = time.time()
    cached = _id_token_cache.get(audience)
    if cached and (now - cached[1]) < 3000:
        return cached[0]
    if now < _metadata_retry_after:
        return None
    req = Request(f"{_METADATA_IDENTITY_URL}?audience={quote(audience, safe='')}",
                  headers={"Metadata-Flavor": "Google"})
    try:
        with urlopen(req, timeout=2) as resp:
            token = resp.read().decode("utf-8").strip()
    except (URLError, OSError) as exc:
        _metadata_retry_after = now + 30
        sys.stderr.write(f"ID-token fetch failed for audience {audience}: {exc}\n")
        return None
    _id_token_cache[audience] = (token, now)
    return token


def call_provider(provider_url: str, method: str, path: str, *,
                  member_token: Optional[str] = None, body: Optional[Any] = None,
                  timeout: int = 20) -> Tuple[int, Any]:
    """Call the provider at `provider_url`; return (status, parsed JSON or None).

    Sends a Google ID token (Cloud Run IAM) when on GCE, and the member's opaque
    LISA token as ``X-Lisa-Token`` when given.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: Dict[str, str] = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if member_token:
        headers["X-Lisa-Token"] = member_token
    token = id_token(provider_url)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(provider_url + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (URLError, OSError) as exc:
        return 502, {"error": f"provider unreachable: {exc}"}
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        sys.stderr.write(
            f"provider {method} {path}: non-JSON response "
            f"(status {status}, auth_sent={bool(token)})\n")
        return 502, {"error": f"invalid response from provider (status {status})"}
