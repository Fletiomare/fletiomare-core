"""Shared config + .env loading for Fletiomare apps.

`CoreConfig` holds the fields every app needs (provider URL, bootstrap admins,
which SPA shell to serve, the /login throttle, and best-effort SendGrid email).
Apps subclass it to add their own fields (e.g. the platform adds Anthropic +
beeldbank settings) and build it in their own `config_from_env()`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Set


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from `path` into the environment.

    Existing environment variables win (so an explicit `export` overrides the
    file). Minimal parser: no interpolation, optional surrounding quotes.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


@dataclass
class CoreConfig:
    provider_url: str
    # Bootstrap "beheer" membership numbers — always admin, cannot be removed
    # in-app, so the club can never lock itself out. Further admins are managed
    # at runtime in the store.
    admin_numbers: Set[str] = field(default_factory=set)
    # Which SPA shell to serve at "/" — each app sets its own (e.g. "index.html").
    app_shell: str = "index.html"
    # Brute-force throttle on /login at this public edge (keyed by client IP +
    # username). After this many failures within the window, 429 until it ages out.
    login_max_failures: int = 5
    login_window: int = 300
    # SendGrid email (best-effort notifications).
    sendgrid_api_key: str = ""
    notify_email: str = ""   # commission inbox
    from_email: str = ""


def core_config_kwargs_from_env() -> dict:
    """Read the shared CoreConfig fields from the environment. Apps merge this
    with their own env reads in `config_from_env()`."""
    provider_url = os.environ.get("PROVIDER_URL")
    if not provider_url:
        raise SystemExit("PROVIDER_URL is required (base URL of the provider service)")
    admins = {n.strip() for n in os.environ.get("CONTENT_ADMIN_NUMBERS", "").split(",") if n.strip()}
    return dict(
        provider_url=provider_url.rstrip("/"),
        admin_numbers=admins,
        login_max_failures=int(os.environ.get("CONTENT_LOGIN_MAX_FAILURES", "5")),
        login_window=int(os.environ.get("CONTENT_LOGIN_WINDOW", "300")),
        sendgrid_api_key=os.environ.get("SENDGRID_API_KEY", ""),
        notify_email=os.environ.get("CONTENT_NOTIFY_EMAIL", ""),
        from_email=os.environ.get("CONTENT_FROM_EMAIL", ""),
    )
