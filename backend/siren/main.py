"""App factory and uvicorn entry.

Env (also used by infra/docker-compose.yml):
  SIREN_DB                 SQLite path (dev default .sandbox/data/siren.sqlite,
                           game.sqlite when mode is game)
  SIREN_CONFIG             config path (default contracts/config.yaml)
  SIREN_MODE               "game" runs the synthetic scenario (game.py), no internet needed
  VALHALLA_URL             routing engine base URL, overrides config.routing.valhalla_url
  SIREN_ROUTING_PROVIDER   "estimate" to run without Valhalla (docs/siren.md §9 dev path)
  SIREN_HOST / SIREN_PORT  uvicorn bind (default 127.0.0.1:8000)

The local prototype UI lives in apps/ (ops + field) and is served from the same process
at /ops and /field — one server, no internet, SQLite dev store.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from siren.api import create_app
from siren.config import DEFAULT_PATH, Config
from siren.game import GameClock, Simulator
from siren.store import Store
from siren.ws import Hub

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / ".sandbox" / "data" / "siren.sqlite"
DEFAULT_GAME_DB = REPO_ROOT / ".sandbox" / "data" / "game.sqlite"
DEFAULT_SEED = REPO_ROOT / "seed"


def build_app(
    db_path: str | Path | None = None,
    config_path: str | Path | None = None,
    seed_dir: str | Path | None = None,
    valhalla_url: str | None = None,
    routing_provider: str | None = None,
    now=None,
    mode: str | None = None,
) -> tuple:
    """Return (app, store, hub) — tests need the store and hub, uvicorn only needs the app."""
    config = Config.load(config_path or os.environ.get("SIREN_CONFIG") or DEFAULT_PATH)
    url = valhalla_url or os.environ.get("VALHALLA_URL")
    if url:
        config = config.replaced("routing.valhalla_url", url)
    provider = routing_provider or os.environ.get("SIREN_ROUTING_PROVIDER")
    if provider:
        config = config.replaced("routing.provider", provider)
    mode = mode or os.environ.get("SIREN_MODE") or str(config.get("app.mode", "real"))
    if str(config.get("app.mode", "real")) != mode:
        config = config.replaced("app.mode", mode)

    db = Path(db_path or os.environ.get("SIREN_DB")
              or (DEFAULT_GAME_DB if mode == "game" else DEFAULT_DB))
    if str(db) != ":memory:":
        db.parent.mkdir(parents=True, exist_ok=True)
        # Game mode uses a scratch database: the virtual clock restarts with the process,
        # so pings/positions from an older process would show impossible ages.
        if mode == "game" and db_path is None and not os.environ.get("SIREN_DB") and db.exists():
            for suffix in ("", "-wal", "-shm"):
                (db.parent / (db.name + suffix)).unlink(missing_ok=True)

    # Virtual clock holder: game mode scales real time by game.speed (UI control).
    clock = {"get": now or (lambda: datetime.now(timezone.utc))}
    store = Store(db, config, now=lambda: clock["get"]())
    store.init_schema()
    store.sync_config_tables()
    store.load_seed(Path(seed_dir or os.environ.get("SIREN_SEED") or DEFAULT_SEED))

    hub = Hub(store, config)
    sim = None
    if mode == "game":
        if now is None:
            clock["get"] = GameClock(speed=lambda: float(store.config.get("game.speed", 1)))
        sim = Simulator(store, config, hub)

    app = create_app(store, config, hub=hub, sim=sim)
    hub.attach(app)

    apps_root = REPO_ROOT / "apps"
    for name, html in (("ops", True), ("field", True), ("db", True), ("shared", False), ("vendor", False),
                       ("data", False)):
        d = apps_root / name
        if d.is_dir():
            app.mount(f"/{name}", StaticFiles(directory=d, html=html), name=name)
    # Real PMTiles basemap archive (scripts/tiles_build.sh) is served by the
    # byte-range route in api.py (/tiles/{name}); PMTiles needs 206 responses.

    return app, store, hub


def run() -> None:
    import uvicorn

    app, _store, _hub = build_app()
    uvicorn.run(app, host=os.environ.get("SIREN_HOST", "127.0.0.1"),
                port=int(os.environ.get("SIREN_PORT", "8000")))


app, store, hub = build_app()


if __name__ == "__main__":
    run()
