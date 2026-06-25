"""fletiomare-core — shared core for Fletiomare apps that sit on top of the
lisa-auth provider (identity + LISA reads).

Provides: the provider client, the /login brute-force throttle, a base config +
.env loader, small date/email helpers, a base HTTP request handler with the
shared auth/proxy/admin/static routes (and an extension hook for app routes),
and a base store with the shared DB plumbing + admins.

Apps (communicatie-platform, velddienst) import this, subclass `BaseHandler`
and the base store, and add their own routes/tables.
"""
from .config import CoreConfig, core_config_kwargs_from_env, load_dotenv
from .limiter import LoginLimiter
from .provider import call_provider, id_token
from .util import new_id, send_email, today, valid_date

__all__ = [
    "CoreConfig", "core_config_kwargs_from_env", "load_dotenv",
    "LoginLimiter",
    "call_provider", "id_token",
    "new_id", "send_email", "today", "valid_date",
]

# server.BaseHandler and store.* are imported lazily by apps (they pull in
# http.server / sqlite) — kept out of the top-level import for now.
