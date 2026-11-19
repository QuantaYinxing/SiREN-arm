"""WebSocket hub per contracts/events.md.

Client sends `position.ping`, `status.set`, `heartbeat`, `snapshot.request`; the server
replies with an event envelope {id, type, ts, v, payload}. Position broadcasts to `/ops`
are coalesced to at most one per member per `position_min_interval` seconds so the map
does not thrash, and position events are not written to the event log (they are ephemeral;
the snapshot carries the latest position).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from siren import state as state_mod
from siren import texts
from siren.api import ACCESS_TOKENS
from siren.config import Config, ConfigError
from siren.state import TransitionError
from siren.store import NotFound, Store

SERVER_TYPES = {
    "incident.created",
    "incident.updated",
    "assignment.updated",
    "member.position",
    "member.status",
    "vehicle.water_level",
    "restriction.reported",
    "restriction.updated",
    "waterSource.updated",
    "config.updated",
}


class Hub:
    def __init__(self, store: Store, config: Config, position_min_interval: float = 2.0) -> None:
        self.store = store
        self.config = config
        self.position_min_interval = position_min_interval
        self.clients: dict[asyncio.Queue, dict] = {}
        self._last_position: dict[str, float] = {}
        self._seq = 0

    # ---- outbound ----

    def _envelope(self, etype: str, payload: dict) -> dict:
        self._seq += 1
        return {"id": f"ev_{self._seq}", "type": etype, "ts": self.store.now_iso(), "v": 1, "payload": payload}

    def broadcast(self, envelope: dict) -> None:
        """Push an envelope to every connected client; non-contract types are dropped."""
        if envelope.get("type") not in SERVER_TYPES:
            return
        for queue in list(self.clients):
            queue.put_nowait(envelope)

    # ---- inbound ----

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        token = ws.query_params.get("token") or ""
        account = ACCESS_TOKENS.get(token)
        meta = {
            "client_id": ws.query_params.get("client_id") or f"c_{uuid.uuid4().hex[:8]}",
            "app": ws.query_params.get("app", "ops"),
            "member_id": account.get("member_id") if account else None,
            "role": account.get("role") if account else None,
        }
        queue: asyncio.Queue = asyncio.Queue()
        self.clients[queue] = meta
        pump = asyncio.create_task(self._pump(ws, queue))
        try:
            while True:
                raw = await ws.receive_text()
                await self._receive(ws, meta, raw)
        except WebSocketDisconnect:
            pass
        finally:
            pump.cancel()
            self.clients.pop(queue, None)

    async def _pump(self, ws: WebSocket, queue: asyncio.Queue) -> None:
        while True:
            await ws.send_json(await queue.get())

    async def _receive(self, ws: WebSocket, meta: dict, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send_json(self._envelope("error", {"message": "payload must be JSON"}))
            return
        etype = msg.get("type")
        payload = msg.get("payload") or {}
        if etype == "snapshot.request":
            await self._snapshot(ws, payload.get("since_event_id"))
        elif etype == "position.ping":
            await self._ping(ws, meta, payload)
        elif etype == "status.set":
            await self._status(ws, meta, payload)
        elif etype == "heartbeat":
            meta["last_heartbeat"] = self.store.now_iso()
        else:
            await ws.send_json(self._envelope("error", {"message": f"unknown event type {etype!r}"}))

    async def _snapshot(self, ws: WebSocket, since: str | None) -> None:
        """Replay buffered events when the gap is covered, otherwise send a full snapshot."""
        replay = self.store.events_since(since) if since else None
        if replay:
            for envelope in replay:
                await ws.send_json(envelope)
            return
        await ws.send_json({"id": f"ev_{self._seq}", "type": "snapshot", "ts": self.store.now_iso(),
                            "v": 1, "payload": self.store.snapshot()})

    async def _ping(self, ws: WebSocket, meta: dict, payload: dict) -> None:
        member_id = payload.get("member_id") or meta.get("member_id")
        position = payload.get("position") or {}
        if not member_id or "lat" not in position or "lon" not in position:
            await ws.send_json(self._envelope("error", {"message": "position.ping needs member_id and position"}))
            return
        try:
            result = self.store.record_ping(
                member_id, float(position["lat"]), float(position["lon"]),
                position.get("accuracy_m"), payload.get("vehicle_id"), payload.get("status"),
            )
        except NotFound:
            await ws.send_json(self._envelope("error", {"message": f"member {member_id} not found"}))
            return
        now = time.monotonic()
        if now - self._last_position.get(member_id, -1e9) >= self.position_min_interval:
            self._last_position[member_id] = now
            self.broadcast(self._envelope("member.position", {
                "member_id": member_id,
                "vehicle_id": payload.get("vehicle_id"),
                "position": result["position"],
                "freshness": result["freshness"],
                "position_age_seconds": result["position_age_seconds"],
            }))

    async def _status(self, ws: WebSocket, meta: dict, payload: dict) -> None:
        member_id = payload.get("member_id") or meta.get("member_id")
        target = payload.get("status")
        if not member_id or not target:
            await ws.send_json(self._envelope("error", {"message": "status.set needs member_id and status"}))
            return
        if meta.get("role") == "officer" and meta.get("member_id") and member_id != meta["member_id"]:
            await ws.send_json(self._envelope("error", {"message": "an officer may only set their own status"}))
            return
        try:
            result = state_mod.apply_member_status(self.config, self.store, member_id, target)
        except (TransitionError, ConfigError, NotFound) as exc:
            allowed = getattr(exc, "allowed", [])
            await ws.send_json(self._envelope("error", {"message": str(exc), "allowed": allowed}))
            return
        self._emit_status(result)

    def _emit_status(self, result: dict) -> None:
        """Mirror what the REST dispatch path emits: member.status, assignment.updated, incident.updated."""
        member = result["member"]
        self.broadcast(self._envelope("member.status", {
            "member_id": member["id"], "status": member["status"],
            "incident_id": (result["assignments"][0]["incident_id"] if result["assignments"] else None),
        }))
        self.store.record_event("member.status", {"member_id": member["id"], "status": member["status"]},
                                texts.member_status(self.config, member), actor_member_id=member["id"])
        incident_ids = {a["incident_id"] for a in result["assignments"]} | {w["incident_id"] for w in result["withdrawn"]}
        for incident_id in sorted(incident_ids):
            incident = self.store.incident(incident_id)
            assignments = [a for a in self.store.assignments(incident_id)
                           if a["status"] in state_mod.ACTIVE_ASSIGNMENT]
            self.broadcast(self._envelope("assignment.updated", {
                "incident_id": incident_id, "assignments": assignments,
            }))
            self.broadcast(self._envelope("incident.updated", {
                "incident_id": incident_id, "status": incident["status"], "incident": incident,
            }))
            self.store.record_event("incident.updated",
                                    {"incident_id": incident_id, "status": incident["status"]},
                                    texts.incident_updated(self.config, incident), incident_id=incident_id)

    def attach(self, app: Any) -> None:
        """Wire the hub onto the FastAPI app at /ws."""

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket) -> None:
            await self.handle(ws)
