"""BaseHandler: the shared HTTP request handler.

Handles the routes every Fletiomare app needs — `/login`, `/logout`, `/me`,
`/api/*` (reverse-proxied to the provider), `/admins` (beheer management),
`/health`, the SPA shell, and static files — and delegates everything else to
`route_get` / `route_post` / `route_put` / `route_delete`, which apps override
to add their own routes (returning True once handled).

`make_server` wires a handler subclass to a config, a store, and a static dir.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

from .limiter import LoginLimiter
from .provider import call_provider
from .store import StoreError

# Cached set of store-managed admin numbers (the resolve path runs on most
# requests, so we avoid a store read every time).
_admin_cache: Set[str] = set()
_admin_cache_at = 0.0
_ADMIN_TTL = 60.0

# Cached email -> roles map from the shadow `users` table (Google sign-in).
_roles_cache: Dict[str, Set[str]] = {}
_roles_cache_at = 0.0

_ASSET_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".woff2": "font/woff2", ".woff": "font/woff", ".otf": "font/otf", ".ttf": "font/ttf",
    ".json": "application/json", ".webmanifest": "application/manifest+json",
    ".map": "application/json",
}


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "fletiomare/1.0"
    app_name = "fletiomare"   # subclasses set this (shows up in /health)

    # -- server-bound accessors ----------------------------------------------
    @property
    def cfg(self):
        return self.server.cfg  # type: ignore[attr-defined]

    @property
    def store(self):
        return self.server.store  # type: ignore[attr-defined]

    @property
    def limiter(self) -> LoginLimiter:
        return self.server.limiter  # type: ignore[attr-defined]

    @property
    def static_dir(self) -> str:
        return self.server.static_dir  # type: ignore[attr-defined]

    # -- helpers --------------------------------------------------------------
    def _client_ip(self) -> str:
        """Best-effort client IP (throttle key only). On Cloud Run the real client
        is the first hop in X-Forwarded-For; fall back to the socket peer."""
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _send(self, status: int, payload: Optional[Any] = None,
              extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        if body:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_bytes(self, data: bytes, ctype: str,
                    extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _read_body(self, max_bytes: int = 5_000_000) -> Optional[bytes]:
        """Raw request body (for file uploads). None if missing/too large."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > max_bytes:
            return None
        return self.rfile.read(length)

    def _member_token(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        return None

    def _member_numbers(self, member: Dict[str, Any]) -> Set[str]:
        nums = set()
        for key in ("club_membership_number", "federation_membership_number"):
            val = member.get(key)
            if val not in (None, ""):
                nums.add(str(val))
        return nums

    def _admin_numbers(self) -> Set[str]:
        """Bootstrap numbers ∪ store-managed admin numbers (cached ~60s)."""
        global _admin_cache, _admin_cache_at
        if time.time() - _admin_cache_at >= _ADMIN_TTL:
            try:
                _admin_cache = {str(a.get("number")) for a in self.store.list_admins()}
                _admin_cache_at = time.time()
            except StoreError:
                pass  # keep the stale set on a transient backend error
        return _admin_cache | self.cfg.admin_numbers

    def _invalidate_admin_cache(self) -> None:
        global _admin_cache_at
        _admin_cache_at = 0.0

    def _user_roles_map(self) -> Dict[str, Set[str]]:
        """email -> roles for enabled shadow users (cached ~60s)."""
        global _roles_cache, _roles_cache_at
        if time.time() - _roles_cache_at >= _ADMIN_TTL:
            try:
                _roles_cache = {str(u.get("email")): set(u.get("roles") or [])
                                for u in self.store.list_users()
                                if u.get("enabled", True)}
                _roles_cache_at = time.time()
            except (StoreError, AttributeError):
                pass  # keep the stale map on a transient backend error
        return _roles_cache

    def _invalidate_user_cache(self) -> None:
        global _roles_cache_at
        _roles_cache_at = 0.0

    def _member_roles(self, member: Dict[str, Any]) -> Set[str]:
        """Roles for a member — from the shadow `users` table by email (Google
        sign-in). LISA members carry no email here, so they get the empty set."""
        email = str(member.get("email") or "").strip().lower()
        return self._user_roles_map().get(email, set()) if email else set()

    def _effective_admin(self, member: Dict[str, Any], provider_is_admin: Any) -> bool:
        return (bool(provider_is_admin)
                or bool(self._member_numbers(member) & self._admin_numbers())
                or "admin" in self._member_roles(member))

    def _resolve_member(self) -> Optional[Tuple[Dict[str, Any], bool]]:
        """Identify the caller via the provider's /me. Sends 401 and returns None
        when there is no valid member token."""
        token = self._member_token()
        if token:
            status, data = call_provider(self.cfg.provider_url, "GET", "/me", member_token=token)
            if status == 200 and isinstance(data, dict):
                member = data.get("member") or {}
                return member, self._effective_admin(member, data.get("is_admin"))
        self._send(401, {"error": "missing or invalid token"})
        return None

    def _require_admin(self) -> Optional[Tuple[Dict[str, Any], bool]]:
        resolved = self._resolve_member()
        if not resolved:
            return None
        _member, is_admin = resolved
        if not is_admin:
            self._send(403, {"error": "admin only"})
            return None
        return resolved

    def log_message(self, fmt, *args):  # concise, never logs bodies/secrets
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- app-route hooks (override in subclasses; return True if handled) ------
    def route_get(self, path: str) -> bool:
        return False

    def route_post(self, path: str) -> bool:
        return False

    def route_put(self, path: str) -> bool:
        return False

    def route_delete(self, path: str) -> bool:
        return False

    # -- dispatch -------------------------------------------------------------
    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path in ("/", "/app", "/index.html"):
                return self._serve_ui()
            if path == "/health":
                return self._send(200, {"status": "ok", "app": self.app_name})
            if path == "/auth-config":
                return self._auth_config()
            if path == "/me":
                return self._me()
            if path == "/api" or path.startswith("/api/"):
                return self._proxy("GET", self.path, with_token=True)  # full path + query
            if path == "/admins":
                return self._list_admins()
            if path == "/users":
                return self._list_users()
            if self.route_get(path):
                return
            return self._serve_static(path)
        except StoreError as exc:
            self._send(502, {"error": f"storage error: {exc}"})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == "/login":
                return self._login()
            if path == "/google-login":
                return self._google_login()
            if path == "/logout":
                return self._proxy("POST", "/logout", with_token=True)
            if path == "/admins":
                return self._add_admin()
            if path == "/users":
                return self._upsert_user()
            if self.route_post(path):
                return
            self._send(404, {"error": "not found"})
        except StoreError as exc:
            self._send(502, {"error": f"storage error: {exc}"})

    def do_PUT(self):
        try:
            path = urlparse(self.path).path
            if self.route_put(path):
                return
            self._send(404, {"error": "not found"})
        except StoreError as exc:
            self._send(502, {"error": f"storage error: {exc}"})

    def do_DELETE(self):
        try:
            path = urlparse(self.path).path
            if path.startswith("/admins/"):
                return self._remove_admin(path.split("/", 2)[2])
            if path.startswith("/users/"):
                return self._remove_user(unquote(path.split("/", 2)[2]))
            if self.route_delete(path):
                return
            self._send(404, {"error": "not found"})
        except StoreError as exc:
            self._send(502, {"error": f"storage error: {exc}"})

    # -- reverse proxy to the provider ----------------------------------------
    def _proxy(self, method: str, path: str, *, with_token: bool = False,
               with_body: bool = False):
        member_token = self._member_token() if with_token else None
        body = None
        if with_body:
            body = self._read_json()
            if not isinstance(body, dict):
                return self._send(400, {"error": "invalid JSON body"})
        status, data = call_provider(self.cfg.provider_url, method, path,
                                     member_token=member_token, body=body)
        self._send(status, data)

    def _login(self):
        """Proxy login to the provider, then override is_admin with the platform's
        effective value (env bootstrap ∪ managed beheerders). Throttled per client
        IP + username to blunt brute-force guessing through this public edge."""
        body = self._read_json()
        if not isinstance(body, dict):
            return self._send(400, {"error": "invalid JSON body"})
        username = str(body.get("username") or "").strip()
        keys = [k for k in (f"ip:{self._client_ip()}", f"user:{username}" if username else "") if k]
        wait = self.limiter.retry_after(*keys)
        if wait:
            return self._send(429, {"error": "te veel inlogpogingen, probeer het later opnieuw"},
                              extra_headers={"Retry-After": str(wait)})
        status, data = call_provider(self.cfg.provider_url, "POST", "/login", body=body)
        if status == 401:
            self.limiter.record_failure(*keys)
        elif status == 200:
            self.limiter.reset(*keys)
            if isinstance(data, dict) and data.get("member") is not None:
                data["is_admin"] = self._effective_admin(data["member"], data.get("is_admin"))
                data["roles"] = sorted(self._member_roles(data["member"]))
        self._send(status, data)

    def _google_login(self):
        """Proxy a Google ID token to the provider, which verifies it (signature,
        audience, @fletiomare.nl domain) and mints a session; then merge the
        platform's effective admin + roles. Throttled per client IP."""
        body = self._read_json()
        if not isinstance(body, dict):
            return self._send(400, {"error": "invalid JSON body"})
        key = f"ip:{self._client_ip()}"
        wait = self.limiter.retry_after(key)
        if wait:
            return self._send(429, {"error": "te veel inlogpogingen, probeer het later opnieuw"},
                              extra_headers={"Retry-After": str(wait)})
        status, data = call_provider(self.cfg.provider_url, "POST", "/google-login", body=body)
        if status in (401, 403):
            self.limiter.record_failure(key)
        elif status == 200:
            self.limiter.reset(key)
            if isinstance(data, dict) and data.get("member") is not None:
                data["is_admin"] = self._effective_admin(data["member"], data.get("is_admin"))
                data["roles"] = sorted(self._member_roles(data["member"]))
        self._send(status, data)

    def _auth_config(self):
        """Public — lets the SPA decide whether to render the Google button."""
        self._send(200, {"google_client_id": self.cfg.google_client_id,
                         "google_allowed_domain": self.cfg.google_allowed_domain})

    def _me(self):
        token = self._member_token()
        status, data = call_provider(self.cfg.provider_url, "GET", "/me", member_token=token)
        if status == 200 and isinstance(data, dict) and data.get("member") is not None:
            data["is_admin"] = self._effective_admin(data["member"], data.get("is_admin"))
            data["roles"] = sorted(self._member_roles(data["member"]))
        self._send(status, data)

    # -- user management (who is "beheer") ------------------------------------
    def _list_admins(self):
        if not self._require_admin():
            return
        self._send(200, {"admins": self.store.list_admins(),
                         "bootstrap": sorted(self.cfg.admin_numbers)})

    def _add_admin(self):
        resolved = self._require_admin()
        if not resolved:
            return
        member, _ = resolved
        body = self._read_json()
        if not isinstance(body, dict):
            return self._send(400, {"error": "invalid JSON body"})
        number = str(body.get("number") or "").strip()
        if not number.isdigit():
            return self._send(400, {"error": "voer een geldig lidnummer in (alleen cijfers)"})
        admin = self.store.add_admin(number, name=(body.get("name") or "").strip(),
                                     added_by=member.get("name") or "")
        self._invalidate_admin_cache()
        self._send(201, admin)

    def _remove_admin(self, number: str):
        if not self._require_admin():
            return
        number = str(number).strip()
        if number in self.cfg.admin_numbers:
            return self._send(400, {"error": "vaste beheerder (via configuratie) — "
                                    "niet in de app te verwijderen"})
        ok = self.store.remove_admin(number)
        self._invalidate_admin_cache()
        if not ok:
            return self._send(404, {"error": "not found"})
        self._send(204)

    # -- shadow users (Google sign-in identities -> roles) --------------------
    def _list_users(self):
        if not self._require_admin():
            return
        self._send(200, {"users": self.store.list_users(),
                         "roles": ["admin", "approver", "reserveerder"]})

    def _upsert_user(self):
        resolved = self._require_admin()
        if not resolved:
            return
        member, _ = resolved
        body = self._read_json()
        if not isinstance(body, dict):
            return self._send(400, {"error": "invalid JSON body"})
        email = str(body.get("email") or "").strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return self._send(400, {"error": "voer een geldig e-mailadres in"})
        roles = sorted({re.sub(r"[^a-z0-9_-]", "", str(r).strip().lower())
                        for r in (body.get("roles") or [])} - {""})
        rec = self.store.upsert_user(
            email, name=(body.get("name") or "").strip(), roles=roles,
            enabled=bool(body.get("enabled", True)),
            added_by=member.get("name") or member.get("email") or "")
        self._invalidate_user_cache()
        self._send(201, rec)

    def _remove_user(self, email: str):
        if not self._require_admin():
            return
        if not self.store.remove_user(str(email).strip()):
            return self._send(404, {"error": "not found"})
        self._invalidate_user_cache()
        self._send(204)

    # -- web UI / static ------------------------------------------------------
    def _serve_ui(self):
        return self._serve_file(os.path.join(self.static_dir, self.cfg.app_shell),
                                "text/html; charset=utf-8")

    def _serve_static(self, path: str):
        full = os.path.normpath(os.path.join(self.static_dir, path.lstrip("/")))
        if not full.startswith(self.static_dir + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        ctype = _ASSET_TYPES.get(os.path.splitext(full)[1].lower())
        if not ctype:
            return self._send(404, {"error": "not found"})
        return self._serve_file(full, ctype)

    def _serve_file(self, full: str, ctype: str):
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        self._send_bytes(body, ctype)


def make_server(handler_class, config, backing_store, static_dir: str,
                host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler_class)
    server.cfg = config             # type: ignore[attr-defined]
    server.store = backing_store    # type: ignore[attr-defined]
    server.static_dir = static_dir  # type: ignore[attr-defined]
    server.limiter = LoginLimiter(config.login_max_failures, config.login_window)  # type: ignore[attr-defined]
    return server
