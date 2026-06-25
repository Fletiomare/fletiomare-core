"""Base store: shared persistence plumbing for two interchangeable backends.

  - ``SqliteBase``    : local dev and tests, no credentials needed.
  - ``FirestoreBase`` : production on Cloud Run — durable, scales to zero, called
                        over the REST API with the runtime SA's token (stdlib-only).

Both provide the connection/REST plumbing and the **admins** ("beheer") table,
which every app shares. Apps subclass these, add their own tables (SQLite: via
``_create_tables``) and methods, and provide their own ``store_from_env()``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)


class StoreError(RuntimeError):
    """Backend failure (e.g. Firestore unreachable)."""


# --------------------------------------------------------------------------- #
# SQLite base (local + tests)
# --------------------------------------------------------------------------- #

class SqliteBase:
    def __init__(self, path: str = "platform.db") -> None:
        self.path = path
        with self._conn() as conn:
            self._create_tables(conn)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Create the shared `admins` table. Subclasses override, call super, and
        add their own tables."""
        conn.execute(
            """CREATE TABLE IF NOT EXISTS admins (
                number TEXT PRIMARY KEY, name TEXT, added_by TEXT, added_at REAL
            )""")

    # -- admins (who is "beheer") --
    def list_admins(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM admins ORDER BY added_at ASC").fetchall()
        return [{"number": r["number"], "name": r["name"], "added_by": r["added_by"],
                 "added_at": r["added_at"]} for r in rows]

    def add_admin(self, number: str, *, name: str = "", added_by: str = "") -> Dict[str, Any]:
        rec = {"number": str(number), "name": name, "added_by": added_by,
               "added_at": time.time()}
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO admins VALUES (?,?,?,?)",
                         (rec["number"], name, added_by, rec["added_at"]))
        return rec

    def remove_admin(self, number: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM admins WHERE number=?",
                                (str(number),)).rowcount > 0


# --------------------------------------------------------------------------- #
# Firestore base (production)
# --------------------------------------------------------------------------- #

class FirestoreBase:
    """Firestore (Native mode) over the REST API, stdlib-only.

    Documents store epoch floats (doubleValue) for times to avoid timestamp
    parsing, and a redundant ``id`` field so listed docs carry their id. Filtering
    and sorting are done in Python (collections here are small)."""

    def __init__(self, project: str, database: str = "(default)") -> None:
        self.project = project
        self.base = (f"https://firestore.googleapis.com/v1/projects/{project}"
                     f"/databases/{database}/documents")
        self._token = ""
        self._token_exp = 0.0

    # -- auth --
    def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp:
            return self._token
        req = Request(f"{_METADATA_TOKEN_URL}?scopes="
                      "https://www.googleapis.com/auth/datastore",
                      headers={"Metadata-Flavor": "Google"})
        try:
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (URLError, OSError) as exc:
            raise StoreError(f"could not fetch access token: {exc}") from exc
        self._token = data["access_token"]
        self._token_exp = now + int(data.get("expires_in", 3600)) - 60
        return self._token

    def _request(self, method: str, path: str,
                 body: Optional[Dict[str, Any]] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=20) as resp:
                raw = resp.read()
        except HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise StoreError(f"Firestore {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError) as exc:
            raise StoreError(f"Firestore unreachable: {exc}") from exc
        return json.loads(raw.decode("utf-8")) if raw else None

    # -- typed-value conversion --
    @classmethod
    def _enc(cls, value: Any) -> Dict[str, Any]:
        if value is None:
            return {"nullValue": None}
        if isinstance(value, bool):
            return {"booleanValue": value}
        if isinstance(value, int):
            return {"integerValue": str(value)}
        if isinstance(value, float):
            return {"doubleValue": value}
        if isinstance(value, str):
            return {"stringValue": value}
        if isinstance(value, (list, tuple)):
            return {"arrayValue": {"values": [cls._enc(v) for v in value]}}
        if isinstance(value, dict):
            return {"mapValue": {"fields": {k: cls._enc(v) for k, v in value.items()}}}
        raise StoreError(f"cannot encode {type(value)!r} for Firestore")

    @classmethod
    def _dec(cls, value: Dict[str, Any]) -> Any:
        if "nullValue" in value:
            return None
        if "booleanValue" in value:
            return value["booleanValue"]
        if "integerValue" in value:
            return int(value["integerValue"])
        if "doubleValue" in value:
            return value["doubleValue"]
        if "stringValue" in value:
            return value["stringValue"]
        if "arrayValue" in value:
            return [cls._dec(v) for v in value["arrayValue"].get("values", [])]
        if "mapValue" in value:
            return {k: cls._dec(v) for k, v in value["mapValue"].get("fields", {}).items()}
        return None

    @classmethod
    def _doc_fields(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"fields": {k: cls._enc(v) for k, v in data.items()}}

    @classmethod
    def _from_doc(cls, doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not doc or "fields" not in doc:
            return None
        return {k: cls._dec(v) for k, v in doc["fields"].items()}

    def _put(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        self._request("PATCH", f"/{collection}/{quote(doc_id, safe='')}",
                      self._doc_fields(data))

    def _get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        return self._from_doc(self._request("GET", f"/{collection}/{quote(doc_id, safe='')}"))

    def _list(self, collection: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = ""
        while True:
            suffix = "?pageSize=300" + (f"&pageToken={quote(page)}" if page else "")
            resp = self._request("GET", f"/{collection}{suffix}") or {}
            for doc in resp.get("documents", []):
                rec = self._from_doc(doc)
                if rec is not None:
                    out.append(rec)
            page = resp.get("nextPageToken", "")
            if not page:
                return out

    def _delete(self, collection: str, doc_id: str) -> None:
        self._request("DELETE", f"/{collection}/{quote(doc_id, safe='')}")

    # -- admins (who is "beheer") --
    def list_admins(self) -> List[Dict[str, Any]]:
        return sorted(self._list("admins"), key=lambda a: a.get("added_at", 0))

    def add_admin(self, number: str, *, name: str = "", added_by: str = "") -> Dict[str, Any]:
        rec = {"number": str(number), "name": name, "added_by": added_by,
               "added_at": time.time()}
        self._put("admins", rec["number"], rec)
        return rec

    def remove_admin(self, number: str) -> bool:
        if not self._get("admins", str(number)):
            return False
        self._delete("admins", str(number))
        return True
