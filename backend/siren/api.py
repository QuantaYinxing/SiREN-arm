"""FastAPI routes implementing contracts/openapi.yaml.

Wire format is snake_case; positions are {lat,lon}. Enum values stay Indonesian (Baru,
Dispatched, IsiUlang, ...) — the UI renders its Indonesian labels from the config dump.
Developer-facing error details are English (docs/rules.md §4); the UI maps them to its own
Indonesian wording. The WebSocket endpoint lives in siren.ws.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from siren import routing as routing_mod
from siren import state as state_mod
from siren import texts
from siren import water as water_mod
from siren.config import Config, ConfigError
from siren.geo import normalize_geometry
from siren.restrictions import VALID_VERIFY_STATUSES
from siren.state import TIMESTAMP_FIELD, TransitionError
from siren.store import Conflict, LinkError, NotFound, Store
from siren.timeutil import add_minutes, today

ACCESS_TOKENS: dict[str, dict] = {}
REFRESH_TOKENS: dict[str, dict] = {}

ACTIVE_STATUSES = ("Baru", "Dispatched", "EnRoute", "OnScene", "Returning")


class AuthError(Exception):
    """Authentication/authorization failure; mapped to 401 or 403."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


class Busy(Exception):
    """A member is already dispatched to another active incident (HTTP 409)."""


class ConfirmRequired(Exception):
    """Refill reset needs confirmation (HTTP 409)."""


def _account(request: Request, required: bool = True, role: str | None = None) -> dict | None:
    """Bearer token session. Reporter endpoints are deliberately unauthenticated."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    account = ACCESS_TOKENS.get(token) if token else None
    if account is None:
        if required:
            raise AuthError(401, "missing or invalid bearer token")
        return None
    if role and account["role"] != role:
        raise AuthError(403, f"role {account['role']} is not allowed")
    return account


def _actor(account: dict | None) -> tuple[str | None, str | None]:
    if not account:
        return None, None
    return account["id"], account.get("member_id")


def dispatch_warnings(config: Config, store: Store, members: list[dict], vehicle_id: str | None) -> list[str]:
    """Operational warnings shown before dispatch; the UI never blocks on them (§5.5)."""
    flags = config.get("dispatch", {}) or {}
    warnings: list[str] = []
    if not vehicle_id and flags.get("warn_missing_vehicle", True):
        warnings.append(texts.warn_missing_vehicle())
    teams = {m["team_id"] for m in members}
    if len(teams) > 1 and flags.get("warn_cross_team", True):
        warnings.append(texts.warn_cross_team(len(teams)))
    if vehicle_id:
        veh = store.vehicle(vehicle_id)
        if veh["status"] != "Standby" and flags.get("warn_vehicle_in_use", True):
            warnings.append(texts.warn_vehicle_in_use(veh["name"], config.label("vehicle_status", veh["status"])))
    if flags.get("warn_stale_gps", True):
        for m in members:
            fresh, age = store.freshness(m["id"])
            if fresh != "live":
                warnings.append(texts.warn_stale_gps(m["name"], fresh, age))
    return warnings


def create_app(store: Store, config: Config | None = None, hub: Any = None, sim: Any = None) -> FastAPI:
    config = config or store.config

    async def water_loop(interval: float = 60.0) -> None:
        """Slow background tick for pump-based level estimates (docs/siren.md §5.4)."""
        import asyncio

        while True:
            await asyncio.sleep(interval)
            changed = water_mod.tick(app.state.config, store)
            for veh in changed:
                state = store.water_state(veh["id"])
                envelope = store.record_event(
                    "vehicle.water_level",
                    {"vehicle_id": veh["id"], "level": state["level"], "source": "estimate",
                     "confirmed_at": state["confirmed_at"]},
                    texts.water_level(app.state.config, veh["name"], state["level"], "estimate"),
                )
                if app.state.hub is not None:
                    app.state.hub.broadcast(envelope)

    async def lifespan(_app: FastAPI):
        import asyncio

        task = asyncio.create_task(water_loop())
        sim_task = asyncio.create_task(app.state.sim.run()) if app.state.sim is not None else None
        try:
            yield
        finally:
            task.cancel()
            if sim_task is not None:
                sim_task.cancel()

    app = FastAPI(title="SiREN API", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.config = config
    app.state.hub = hub
    app.state.sim = sim

    def _error(status: int, kind: str, detail: str, allowed: list[Any] | None = None) -> JSONResponse:
        """Error body per contracts/openapi.yaml Error schema."""
        body: dict[str, Any] = {"error": kind, "detail": detail}
        if allowed:
            body["allowed"] = allowed
        return JSONResponse(status_code=status, content=body)

    @app.exception_handler(AuthError)
    async def _on_auth(_request: Request, exc: AuthError) -> JSONResponse:
        return _error(exc.status, "auth", str(exc))

    @app.exception_handler(TransitionError)
    async def _on_transition(_request: Request, exc: TransitionError) -> JSONResponse:
        return _error(409, "transition_not_allowed", str(exc), exc.allowed)

    @app.exception_handler(ConfigError)
    async def _on_config(_request: Request, exc: ConfigError) -> JSONResponse:
        return _error(400, "invalid_value", str(exc), exc.allowed)

    @app.exception_handler(Busy)
    async def _on_busy(_request: Request, exc: Busy) -> JSONResponse:
        return _error(409, "already_dispatched", str(exc))

    @app.exception_handler(ConfirmRequired)
    async def _on_confirm(_request: Request, exc: ConfirmRequired) -> JSONResponse:
        return _error(409, "confirmation_required", str(exc))

    @app.exception_handler(Conflict)
    async def _on_conflict(_request: Request, exc: Conflict) -> JSONResponse:
        return _error(400, "conflict", str(exc))

    @app.exception_handler(NotFound)
    async def _on_not_found(_request: Request, exc: NotFound) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(LinkError)
    async def _on_link(_request: Request, exc: LinkError) -> JSONResponse:
        return _error(410, "link_unusable", str(exc))

    @app.exception_handler(routing_mod.RoutingError)
    async def _on_routing(_request: Request, exc: routing_mod.RoutingError) -> JSONResponse:
        return _error(400, "routing_failed", str(exc))

    @app.exception_handler(ValueError)
    async def _on_value(_request: Request, exc: ValueError) -> JSONResponse:
        return _error(400, "invalid_input", str(exc))

    def emit(request: Request, etype: str, payload: dict, text: str,
             account: dict | None = None, incident_id: str | None = None) -> None:
        acc_id, mem_id = _actor(account)
        envelope = store.record_event(etype, payload, text, actor_account_id=acc_id,
                                      actor_member_id=mem_id, incident_id=incident_id)
        if app.state.hub is not None:
            app.state.hub.broadcast(envelope)

    # ---- live state ----

    @app.get("/snapshot")
    def snapshot(request: Request) -> dict:
        return store.snapshot()

    @app.get("/log")
    def log(request: Request, incident_id: str | None = None, limit: int = 50) -> list[dict]:
        return store.event_log(limit=limit, incident_id=incident_id)

    # ---- auth ----

    @app.post("/auth/login")
    def login(request: Request, body: dict = Body(...)) -> dict:
        account = store.authenticate(body.get("username", ""), body.get("password", ""))
        if account is None:
            raise AuthError(401, "wrong username or password")
        access = secrets.token_urlsafe(24)
        refresh = secrets.token_urlsafe(24)
        ACCESS_TOKENS[access] = account
        REFRESH_TOKENS[refresh] = account
        emit(request, "auth.login", {"username": account["username"], "role": account["role"]},
             texts.login(account), account)
        return {"token": access, "refresh_token": refresh, "role": account["role"],
                "member_id": account.get("member_id")}

    @app.post("/auth/refresh")
    def refresh(body: dict = Body(...)) -> dict:
        account = REFRESH_TOKENS.get(body.get("refresh_token", ""))
        if account is None:
            raise AuthError(401, "refresh token unknown")
        access = secrets.token_urlsafe(24)
        ACCESS_TOKENS[access] = account
        return {"token": access, "refresh_token": body["refresh_token"]}

    # ---- accounts (DB manager GUI) ----

    @app.get("/accounts")
    def accounts(request: Request) -> list[dict]:
        _account(request, role="operator")
        return store.accounts()

    @app.post("/accounts", status_code=201)
    def create_account(request: Request, body: dict = Body(...)) -> dict:
        actor = _account(request, role="operator")
        username, password = body.get("username"), body.get("password")
        if not username or not password:
            raise ValueError("username and password are required")
        role = body.get("role", "officer")
        config.validate("auth.roles", role)
        if body.get("member_id"):
            store.member(body["member_id"])
        row = store.create_account(username, role, password, body.get("member_id"))
        emit(request, "account.updated", {"username": row["username"], "role": row["role"]},
             f"Akun {row['username']} ({row['role']}) dibuat", account=actor)
        return row

    @app.patch("/accounts/{id}")
    def update_account(request: Request, id: str, body: dict = Body(...)) -> dict:
        actor = _account(request, role="operator")
        if "role" in body:
            config.validate("auth.roles", body["role"])
        if body.get("member_id"):
            store.member(body["member_id"])
        row = store.update_account(id, body)
        emit(request, "account.updated", {"username": row["username"]},
             f"Akun {row['username']} diubah", account=actor)
        return row

    @app.delete("/accounts/{id}")
    def delete_account(request: Request, id: str) -> dict:
        actor = _account(request, role="operator")
        row = store.delete_account(id)
        emit(request, "account.updated", {"username": row["username"], "removed": True},
             f"Akun {row['username']} dihapus", account=actor)
        return {"username": row["username"], "removed": True}

    @app.delete("/members/{id}")
    def remove_member(request: Request, id: str) -> dict:
        actor = _account(request, role="operator")
        busy = [a for a in store.assignments() if a["member_id"] == id and a["status"] in state_mod.ACTIVE_ASSIGNMENT]
        if busy:
            raise Busy(f"member {id} masih ditugaskan di {busy[0]['incident_id']}")
        row = store.delete_member(id)
        emit(request, "member.updated", {"member_id": id, "name": row["name"], "removed": True},
             f"Anggota {row['name']} dihapus", account=actor)
        return {"id": id, "name": row["name"], "removed": True}

    # ---- config ----

    @app.get("/config")
    def config_dump(request: Request) -> dict:
        return app.state.config.dump()

    @app.get("/config/{key}")
    def config_get(request: Request, key: str) -> dict:
        value = app.state.config.get(key)
        if value is None:
            raise NotFound(f"unknown config key {key}")
        return {"key": key, "value": value}

    # ---- PMTiles basemap (byte-range serving; the pmtiles protocol needs 206) ----

    @app.get("/tiles/{name}")
    def tiles_range(request: Request, name: str):
        from pathlib import Path

        import mimetypes

        from fastapi.responses import Response

        # Only plain filenames — no traversal out of the tiles dir.
        if "/" in name or ".." in name:
            raise NotFound(f"unknown tile archive {name}")
        path = Path(__file__).resolve().parents[2] / ".sandbox" / "data" / "tiles" / name
        if not path.is_file():
            raise NotFound(f"unknown tile archive {name}")
        size = path.stat().st_size
        range_header = request.headers.get("range")
        start, end = 0, size - 1
        status = 200
        if range_header and range_header.startswith("bytes="):
            first = range_header[len("bytes="):].split(",")[0]
            lo, _, hi = first.partition("-")
            if not lo:  # suffix range: last N bytes
                start = max(0, size - int(hi))
                end = size - 1
            else:
                start = int(lo)
                if hi:
                    end = min(int(hi), size - 1)
            status = 206
        with path.open("rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        headers = {
            "Content-Type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(body)),
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(content=body, status_code=status, headers=headers)

    @app.put("/config/{key}")
    def config_put(request: Request, key: str, body: dict = Body(...)) -> dict:
        nonlocal config
        account = _account(request, role="operator")
        if key == "game.speed":
            config.validate("game_speed", int(body.get("value", 1)))
        config = config.replaced(key, body.get("value"))
        store.config = config
        app.state.config = config
        if app.state.hub is not None:
            app.state.hub.config = config
        store.sync_config_tables()
        emit(request, "config.updated", {"key": key, "value": body.get("value")},
             texts.config_updated(key, body.get("value")), account)
        return {"key": key, "value": body.get("value")}

    # ---- master data ----

    @app.get("/teams")
    def teams(request: Request) -> list[dict]:
        return store.teams()

    @app.post("/teams", status_code=201)
    def create_team(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request, role="operator")
        row = store.create_team(body)
        emit(request, "config.updated", {"kind": "team", "team": row},
             texts.master_data("regu", "ditambah", row["name"]), account)
        return row

    @app.get("/members")
    def members(request: Request, team_id: str | None = None) -> list[dict]:
        return store.members(team_id=team_id)

    @app.post("/members", status_code=201)
    def create_member(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request, role="operator")
        row = store.create_member(body)
        emit(request, "config.updated", {"kind": "member", "member": row},
             texts.master_data("anggota", "ditambah", row["name"]), account)
        return row

    @app.get("/members/{id}")
    def member(request: Request, id: str) -> dict:
        return store.member(id)

    @app.patch("/members/{id}")
    def update_member(request: Request, id: str, body: dict = Body(...)) -> dict:
        account = _account(request)
        target = body.get("status")
        if target is not None:
            rest = {k: v for k, v in body.items() if k != "status"}
            result = state_mod.apply_member_status(config, store, id, target)
            if rest:
                store.update_member(id, rest)
            row = store.member(id)
            emit(request, "member.status", {"member_id": row["id"], "status": row["status"]},
                 texts.member_status(config, row), account)
            incident_ids = {a["incident_id"] for a in result["assignments"]} | {w["incident_id"] for w in result["withdrawn"]}
            for incident_id in sorted(incident_ids):
                inc = store.incident(incident_id)
                active = [a for a in store.assignments(incident_id) if a["status"] in state_mod.ACTIVE_ASSIGNMENT]
                emit(request, "assignment.updated", {"incident_id": incident_id, "assignments": active},
                     texts.assignment_updated(config, incident_id, None, [row["name"]], None), account, incident_id)
                emit(request, "incident.updated", {"incident_id": incident_id, "status": inc["status"], "incident": inc},
                     texts.incident_updated(config, inc), account, incident_id)
            return row
        row = store.member(id)
        row = store.update_member(id, body)
        emit(request, "config.updated", {"kind": "member", "member": row},
             texts.master_data("anggota", "diubah", row["name"]), account)
        return row

    @app.get("/vehicle-profiles")
    def profiles(request: Request) -> list[dict]:
        return store.vehicle_profiles()

    @app.post("/vehicle-profiles", status_code=201)
    def create_profile(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request, role="operator")
        row = store.create_profile(body)
        emit(request, "config.updated", {"kind": "vehicle_profile", "vehicle_profile": row},
             texts.master_data("profil kendaraan", "ditambah", row["name"]), account)
        return row

    @app.get("/vehicles")
    def vehicles(request: Request) -> list[dict]:
        return store.vehicles()

    @app.post("/vehicles", status_code=201)
    def create_vehicle(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request, role="operator")
        row = store.create_vehicle(body)
        emit(request, "config.updated", {"kind": "vehicle", "vehicle": row},
             texts.master_data("kendaraan", "ditambah", row["name"]), account)
        return row

    @app.get("/vehicles/{id}")
    def vehicle(request: Request, id: str) -> dict:
        return store.vehicle(id)

    @app.patch("/vehicles/{id}")
    def update_vehicle(request: Request, id: str, body: dict = Body(...)) -> dict:
        account = _account(request)
        row = store.vehicle(id)
        data = dict(body)
        target = data.get("status")
        if target is not None:
            state_mod.check(config, "vehicle", row["status"], target)
            if target == "Standby":
                data.setdefault("eta_minutes", None)
        row = store.update_vehicle(id, data)
        emit(request, "config.updated", {"kind": "vehicle", "vehicle": row},
             texts.master_data("kendaraan", "diubah", row["name"]), account)
        return row

    @app.get("/water-sources")
    def water_sources(request: Request, status: str = "all") -> list[dict]:
        if status != "all":
            config.validate("water_source_status", status)
        return store.water_sources(status=status)

    @app.post("/water-sources", status_code=201)
    def create_water_source(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request, role="operator")
        if "type" not in body or "lat" not in body or "lon" not in body or "name" not in body:
            raise ValueError("name, type, lat and lon are required")
        config.validate("water_source_types", body["type"])
        config.validate("water_source_status", body.get("status", "active"))
        row = store.create_water_source(body)
        emit(request, "waterSource.updated", {"water_source": row},
             texts.water_source_updated(config, row), account)
        return row

    @app.get("/water-sources/{id}")
    def water_source(request: Request, id: str) -> dict:
        return store.water_source(id)

    @app.patch("/water-sources/{id}")
    def update_water_source(request: Request, id: str, body: dict = Body(...)) -> dict:
        account = _account(request)
        if "status" in body:
            config.validate("water_source_status", body["status"])
        row = store.update_water_source(id, body)
        emit(request, "waterSource.updated", {"water_source": row},
             texts.water_source_updated(config, row), account)
        return row

    @app.delete("/water-sources/{id}")
    def remove_water_source(request: Request, id: str) -> dict:
        account = _account(request, role="operator")
        row = store.remove_water_source(id)
        emit(request, "waterSource.updated", {"water_source": row, "removed": True},
             texts.water_source_updated(config, row, removed=True), account)
        return row

    # ---- incidents ----

    @app.get("/incidents")
    def incidents(request: Request, status: str = "active") -> list[dict]:
        if status == "active":
            return store.incidents(status="active")
        config.validate("incident_status", status)
        return store.incidents(status=status)

    @app.post("/incidents", status_code=201)
    def create_incident(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request)
        row = store.create_incident(body)
        emit(request, "incident.created", {"incident": row}, texts.incident_created(config, row),
             account, row["id"])
        return row

    @app.get("/incidents/{id}")
    def incident(request: Request, id: str) -> dict:
        return store.incident(id)

    @app.patch("/incidents/{id}")
    def patch_incident(request: Request, id: str, body: dict = Body(...)) -> dict:
        account = _account(request)
        inc = store.incident(id)
        data = dict(body)
        target = data.get("status")
        if target is not None:
            state_mod.check(config, "incident", inc["status"], target)
            field = TIMESTAMP_FIELD.get(target)
            if field and not inc.get(field):
                data[field] = store.now_iso()
        row = store.update_incident(id, data)
        if target is not None:
            emit(request, "incident.updated", {"incident_id": row["id"], "status": row["status"], "incident": row},
                 texts.incident_updated(config, row), account, row["id"])
        return row

    # ---- dispatch ----

    @app.post("/incidents/{id}/assignments", status_code=201)
    def dispatch(request: Request, id: str, body: dict = Body(...)) -> dict:
        account = _account(request)
        inc = store.incident(id)
        member_ids = list(body.get("members") or [])
        if not member_ids:
            raise Conflict("at least one member is required")
        if len(member_ids) > int(config.get("dispatch.max_members_per_assignment", 12)):
            raise Conflict("too many members in one assignment")
        busy = sorted(
            f"member {store.member(a['member_id'])['name']} is already dispatched to {a['incident_id']}"
            for a in store.assignments()
            if a["status"] in state_mod.ACTIVE_ASSIGNMENT
            and a["member_id"] in member_ids
            and a["incident_id"] != id
        )
        if busy:
            raise Busy("; ".join(sorted(set(busy))))
        members = [store.member(mid) for mid in member_ids]
        vehicle_id = body.get("vehicle_id")
        if vehicle_id:
            store.vehicle(vehicle_id)
        water_source_id = body.get("water_source_id")
        if water_source_id:
            store.water_source(water_source_id)

        warnings = dispatch_warnings(config, store, members, vehicle_id)
        assignments = store.create_assignments(id, member_ids, vehicle_id, water_source_id,
                                               body.get("roles"))

        for mid in member_ids:
            state_mod.apply_member_status(config, store, mid, "Dispatched")

        route = None
        if vehicle_id:
            try:
                route = routing_mod.build_route(config, store, vehicle_id,
                                                {"lat": inc["lat"], "lon": inc["lon"]}, water_source_id)
                store.update_vehicle(vehicle_id, {"eta_minutes": route["total_minutes"]})
                warnings.extend(route.get("warnings", []))
            except routing_mod.RoutingError as exc:
                warnings.append(f"route not planned: {exc}")

        updated = [a for a in store.assignments(id) if a["status"] in state_mod.ACTIVE_ASSIGNMENT]
        veh_row = store.vehicle(vehicle_id) if vehicle_id else None
        names = [m["name"] for m in members]
        ws_name = store.water_source(water_source_id)["name"] if water_source_id else None
        emit(request, "assignment.updated",
             {"incident_id": id, "assignments": updated, "vehicle_id": vehicle_id,
              "water_source_id": water_source_id},
             texts.dispatch_sent(config, inc, veh_row["name"] if veh_row else None, names, ws_name, len(names)),
             account, id)
        for m in members:
            row = store.member(m["id"])
            emit(request, "member.status", {"member_id": row["id"], "status": row["status"], "incident_id": id},
                 texts.member_status(config, row), account, id)
        inc_row = store.incident(id)
        emit(request, "incident.updated", {"incident_id": id, "status": inc_row["status"], "incident": inc_row},
             texts.incident_updated(config, inc_row), account, id)
        return {"incident": inc_row, "assignments": updated, "warnings": warnings, "route": route}

    @app.patch("/assignments/{id}")
    def patch_assignment(request: Request, id: str, body: dict = Body(...)) -> dict:
        account = _account(request)
        row = store.update_assignment(id, body)
        emit(request, "assignment.updated", {"incident_id": row["incident_id"], "assignments": [row]},
             texts.assignment_updated(config, row["incident_id"], None, [store.member(row["member_id"])["name"]], None),
             account, row["incident_id"])
        return row

    @app.delete("/assignments/{id}")
    def withdraw(request: Request, id: str) -> dict:
        account = _account(request)
        row = store.withdraw_assignment(id)
        state_mod.recompute_incident(config, store, row["incident_id"])
        if row["vehicle_id"]:
            state_mod.recompute_vehicle(config, store, row["vehicle_id"])
        store.update_member(row["member_id"], {"status": "Standby", "updated_at": store.now_iso()})
        emit(request, "assignment.updated", {"incident_id": row["incident_id"], "assignments": [row]},
             texts.assignment_updated(config, row["incident_id"], None, [store.member(row["member_id"])["name"]], None),
             account, row["incident_id"])
        emit(request, "member.status", {"member_id": row["member_id"], "status": "Standby"},
             texts.member_status(config, store.member(row["member_id"])), account, row["incident_id"])
        return row

    # ---- routing ----

    @app.post("/routing/route")
    def route(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request)
        if "vehicle_id" not in body or "to" not in body:
            raise ValueError("vehicle_id and to are required")
        return routing_mod.build_route(config, store, body["vehicle_id"], body["to"],
                                       body.get("via_water_source_id"), body.get("restrictions"))

    @app.post("/routing/water-source-options")
    def water_source_options(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request)
        if "vehicle_id" not in body or "incident_id" not in body:
            raise ValueError("vehicle_id and incident_id are required")
        out = routing_mod.water_options(config, store, body["vehicle_id"], body["incident_id"])
        if account:
            store.record_event("routing.options",
                              {"vehicle_id": out["vehicle_id"], "incident_id": out["incident_id"],
                               "options": [o["water_source_id"] for o in out["options"]]},
                              texts.assignment_updated(config, out["incident_id"], None, [], out["options"][0]["name"] if out["options"] else None),
                              actor_account_id=account["id"], incident_id=out["incident_id"])
        return out

    # ---- water ----

    @app.post("/vehicles/{id}/water-level")
    def set_water_level(request: Request, id: str, body: dict = Body(...)) -> dict:
        account = _account(request)
        if "level" not in body:
            raise ValueError("level is required")
        level = config.validate_level(body["level"])
        source = body.get("source", "officer")
        if source not in config.enum_values("water_level.reading_sources"):
            raise ConfigError(f"{source!r} is not a valid reading source",
                              allowed=config.enum_values("water_level.reading_sources"))
        row = store.record_water_level(id, level, source)
        emit(request, "vehicle.water_level",
             {"vehicle_id": id, "level": level, "source": source, "confirmed_at": store.now_iso()},
             texts.water_level(config, row["name"], level, source), account)
        return row

    @app.post("/vehicles/{id}/refill-done")
    def refill_done(request: Request, id: str, body: dict = Body(default={}) ) -> dict:
        account = _account(request)
        if not bool(body.get("confirmed", True)):
            raise ConfirmRequired("refill reset needs confirmation")
        row = store.refill_done(id, True)
        state = store.water_state(id)
        emit(request, "vehicle.water_level",
             {"vehicle_id": id, "level": state["level"], "source": state["source"], "confirmed_at": state["confirmed_at"]},
             texts.refill_done(config, row["name"]), account)
        return row

    # ---- restrictions ----

    @app.get("/road-restrictions")
    def restrictions(request: Request, status: str = "all") -> list[dict]:
        if status != "all":
            config.validate("restriction_status", status)
        store.expire_overdue()
        return store.restrictions(status=status)

    @app.post("/road-restrictions", status_code=201)
    def create_restriction(request: Request, body: dict = Body(...)) -> dict:
        account = _account(request)
        if "kind" not in body or "reason" not in body or "geometry" not in body:
            raise ValueError("kind, reason and geometry are required")
        config.validate("restriction_kinds", body["kind"])
        config.validate("restriction_reason", body["reason"])
        config.validate("restriction_affects", body.get("affected_vehicle_type", "truck"))
        geometry = normalize_geometry(body["geometry"])
        row = store.create_restriction({
            "kind": body["kind"],
            "geometry": geometry,
            "reason": body["reason"],
            "affected_vehicle_type": body.get("affected_vehicle_type", "truck"),
            "status": "pending",
            "reported_by": account["username"] if account else None,
            "reported_member_id": account.get("member_id") if account else None,
            "expires_at": body.get("expires_at"),
            "note": body.get("note"),
            "photo_url": body.get("photo_url"),
        })
        reporter = store.member(row["reported_member_id"])["name"] if row.get("reported_member_id") else (
            account["username"] if account else "operator")
        emit(request, "restriction.reported", {"restriction": row},
             texts.restriction_reported(config, reporter, row["reason"]), account)
        return row

    @app.post("/road-restrictions/{id}/verify")
    def verify_restriction(request: Request, id: str, body: dict = Body(...)) -> dict:
        account = _account(request, role="operator")
        status = body.get("status")
        if status not in VALID_VERIFY_STATUSES:
            raise ConfigError(f"{status!r} is not a valid verification status",
                              allowed=sorted(VALID_VERIFY_STATUSES))
        row = store.verify_restriction(id, status, body.get("note"),
                                       verified_by=account["id"] if account else None)
        emit(request, "restriction.updated", {"restriction_id": id, "status": status, "restriction": row},
             texts.restriction_updated(config, row["reason"], status), account)
        return row

    # ---- reporter links ----

    @app.post("/incidents/{id}/reporter-link")
    def reporter_link(request: Request, id: str, body: dict = Body(default={})) -> dict:
        account = _account(request)
        ttl = body.get("ttl_minutes") or int(config.get("reporter_link.ttl_minutes", 30))
        row = store.create_reporter_link(id, ttl, account["id"] if account else None)
        store.record_event("reporter.link", {"incident_id": id, "token": row["token"], "url": row["url"]},
                           texts.reporter_link_created(config, id, row["url"]),
                           actor_account_id=account["id"] if account else None, incident_id=id)
        return row

    @app.post("/reporter/{token}/location")
    def reporter_location(request: Request, token: str, body: dict = Body(...)) -> dict:
        try:
            inc = store.consume_reporter_link(token, body["lat"], body["lon"], body.get("accuracy_m"),
                                              body.get("name"))
        except KeyError:
            raise LinkError("reporter link not found") from None
        emit(request, "incident.updated", {"incident_id": inc["id"], "status": inc["status"], "incident": inc},
             texts.reporter_location_received(config, inc["id"]), None, inc["id"])
        return inc

    # ---- game mode (dev-only; real mode has no simulator) ----

    @app.post("/game/incident", status_code=201)
    def game_incident(request: Request) -> dict:
        if app.state.sim is None:
            raise NotFound("game mode is not enabled")
        return app.state.sim.spawn_now()

    @app.get("/game/state")
    def game_state(request: Request) -> dict:
        """Dev-only view: is the simulator running, at what speed, how many ticks."""
        speed = int(config.get("game.speed", 1))
        if app.state.sim is None:
            return {"mode": "real", "speed": speed, "ticks": 0, "virtual_now": store.now_iso()}
        return {"mode": "game", "speed": speed, "ticks": app.state.sim.ticks,
                "virtual_now": store.now_iso()}

    # ---- stats ----

    @app.get("/stats/today")
    def stats(request: Request, date: str | None = None) -> dict:
        return store.stats_today(date=date or store.now_iso().split("T")[0])

    return app
