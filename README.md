# fletiomare-core

Shared core for Fletiomare apps that sit on top of the **lisa-auth provider**
(identity + LISA reads). Extracted from the communicatie platform so that the
**communicatie-platform** and **velddienst** apps can each be their own repo
while sharing one well-tested core.

Install (from git, no auth needed — this repo is public, holds no secrets):

```
pip install "fletiomare-core @ git+https://github.com/Fletiomare/fletiomare-core.git@main"
```

## What's in here

| module | purpose | status |
|---|---|---|
| `provider.py` | provider client (`call_provider`, Google `id_token`) | ✅ |
| `limiter.py`  | `LoginLimiter` — /login brute-force throttle | ✅ |
| `config.py`   | `CoreConfig` + `.env` loader + shared env reads | ✅ |
| `util.py`     | Amsterdam dates, ids, best-effort SendGrid email | ✅ |
| `server.py`   | `BaseHandler` — shared helpers + auth/proxy/`/login`/`/me`/`/admins`/static routes, with an app-route extension hook; `make_server` | ✅ |
| `store.py`    | base store: `SqliteBase` + `FirestoreBase` (DB plumbing + admins); apps add their tables/methods | ✅ |

## How apps use it

```python
from fletiomare_core import CoreConfig, call_provider
from fletiomare_core.server import BaseHandler

class Handler(BaseHandler):
    def route_get(self, path):   # return True if handled
        ...
    def route_post(self, path):
        ...
```

Each app keeps its own `static/` (SPA shell + assets), `Dockerfile`,
`cloudbuild.yaml`, and `config_from_env()` (merging `core_config_kwargs_from_env()`
with its own fields).
