"""Small shared helpers: Amsterdam dates, ids, and best-effort email."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_AMS = ZoneInfo("Europe/Amsterdam")


def new_id() -> str:
    return uuid.uuid4().hex


def today() -> str:
    return datetime.now(_AMS).strftime("%Y-%m-%d")


def valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def send_email(cfg, to: Optional[str], subject: str, text: str) -> bool:
    """Send a plain-text email via the SendGrid v3 API. Best-effort: returns False
    (and logs) if unconfigured or on failure — never raises into the request.

    `cfg` must expose `sendgrid_api_key`, `from_email`, and `notify_email`.
    """
    if not (cfg.sendgrid_api_key and cfg.from_email and to):
        return False
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": cfg.from_email, "name": "Communicatie Fletiomare"},
        "reply_to": {"email": cfg.notify_email or cfg.from_email},
        "subject": subject,
        "content": [{"type": "text/plain", "value": text}],
    }
    req = Request("https://api.sendgrid.com/v3/mail/send",
                  data=json.dumps(payload).encode("utf-8"), method="POST",
                  headers={"Authorization": "Bearer " + cfg.sendgrid_api_key,
                           "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except (HTTPError, URLError, OSError) as exc:
        sys.stderr.write(f"sendgrid send to {to} failed: {exc}\n")
        return False
