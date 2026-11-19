"""App factory and uvicorn entry.

Env (also used by infra/docker-compose.yml):
  SIREN_DB                 SQLite path (dev default .sandbox/data/siren.sqlite)
  SIREN_CONFIG             config path (default contracts/config.yaml)
  VALHALLA_URL             routing engine base URL, overrides config.routing.valhalla_url
  SIREN_ROUTING_PROVIDER   "estimate" to run without Valhalla (docs/siren.md §9 dev path)
  SIREN_HOST / SIREN_PORT  uvicorn bind (default 127.0.0.1:8000)
"""

from __future__ import annotations

import os
import pathlib
from pathlib import Path

from siren.api import create_app
from siren.config import DEFAULT_PATH, Config
from siren.store import Store
from siren.ws import Hub

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / ".sandbox" / "data" / "siren.sqlite"
DEFAULT_SEED = REPO_ROOT / "seed"


def build_app(
    db_path: str | Path | None = None,
    config_path: str | Path | None = None,
    seed_dir: str | Path | None = None,
    valhalla_url: str | None = None,
    routing_provider: str | None = None,
    now=None,
) -> tuple:
    """Return (app, store, hub) — tests need the store and hub, uvicorn only needs the app."""
    config = Config.load(config_path or os.environ.get("SIREN_CONFIG") or DEFAULT_PATH)
    url = valhalla_url or os.environ.get("VALHALLA_URL")
    if url:
        config = config.replaced("routing.valhalla_url", url)
    provider = routing_provider or os.environ.get("SIREN_ROUTING_PROVIDER")
    if provider:
        config = config.replaced("routing.provider", provider)

    db = Path(db_path or os.environ.get("SIREN_DB") or DEFAULT_DB)
    if str(db) != ":memory:":
        db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db, config, now=now)
    store.init_schema()
    store.sync_config_tables()
    store.load_seed(Path(seed_dir or os.environ.get("SIREN_SEED") or DEFAULT_SEED))

    hub = Hub(store, config)
    app = create_app(store, config, hub=hub)
    hub.attach(app)
    return app, store, hub


def run() -> None:
    import uvicorn

    app, _store, _hub = build_app()
    uvicorn.run(app, host=os.environ.get("SIREN_HOST", "127.0.0.1"),
                port=int(os.environ.get("SIREN_PORT", "8000")))


app, store, hub = build_app()


if __name__ == "__main__":
    run()
