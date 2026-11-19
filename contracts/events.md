# SiREN WebSocket event contract

Machine-readable form: `contracts/events.json` (envelope + payload schemas,
`schema_version: 1`). This file is the human explanation of the same rules.

## Envelope

Every event, both directions:

```json
{"id": "ev_101", "type": "incident.updated", "ts": "2026-11-19T07:32:04Z", "v": 1, "payload": {...}}
```

- `id` — server-assigned, strictly increasing per server process. Clients keep the
  highest seen id and use it for reconnect (`snapshot.request.since_event_id`).
- `ts` — server clock, UTC ISO-8601.
- `v` — payload schema version; a client that does not understand `v` must ignore the
  event and request a snapshot.
- Payload enum values (`status`, `level`, `reason`, `kind`, `affected_vehicle_type`)
  come from `contracts/config.yaml`, never hard-coded in components (D6). UI labels are
  Indonesian (`label`), glosses are English (`gloss`).

## Server → client

| Type | When it fires | Payload |
| --- | --- | --- |
| `incident.created` | incident created (operator form or reporter link received) | full `incident` |
| `incident.updated` | status or field change on an incident | `incident_id`, `status`, full `incident` |
| `assignment.updated` | dispatch created, patched, or withdrawn | `incident_id`, current `assignments`, optional `vehicle_id` / `water_source_id` |
| `member.position` | a position ping was accepted | `member_id`, optional `vehicle_id`, `position`, `freshness`, `position_age_seconds` |
| `member.status` | duty status changed (REST or `status.set`) | `member_id`, `status`, optional `incident_id` |
| `vehicle.water_level` | manual reading or automatic estimate tick | `vehicle_id`, `level` (0-4), `source`, `confirmed_at` |
| `restriction.reported` | officer/operator marked a road | `restriction` (status `pending`) |
| `restriction.updated` | verification, rejection, or expiry | `restriction_id`, `status`, full `restriction` |
| `waterSource.updated` | master data create/edit/disable | `water_source`, `removed: true` on delete |
| `config.updated` | a config key changed | `key`, `value` |

## Client → server

| Type | Sent by | Notes |
| --- | --- | --- |
| `position.ping` | `/field` (and `/ops` vehicle telemetry later) | `member_id`, `position {lat,lon,accuracy_m}`, optional `vehicle_id` and `status` |
| `status.set` | `/field` big button + quick actions | `member_id`, `status`; server validates the transition and may cascade incident/vehicle status |
| `heartbeat` | both apps, every 30 s | `client_id`, `app`, optional `member_id`; keeps the socket and freshness bookkeeping honest |
| `snapshot.request` | on connect and on reconnect | optional `since_event_id` |

## Freshness

Freshness is separate from duty status (siren.md §6): `live`, `stale`, `offline`,
thresholds from config (`position.stale_after_seconds`,
`position.offline_after_seconds`). Server computes it from the newest ping; clients
never declare it themselves. UI always shows the age ("posisi 3 mnt lalu").

## Reconnect semantics

1. Client connects and sends `snapshot.request` with `since_event_id` if it has one.
2. Server replies `{"type": "snapshot", "payload": {...}}` (shape `events.json`
   `$defs/snapshot`) plus `last_event_id`.
3. If `since_event_id` is still in the retained event buffer **and** there are buffered
   events after it, the server replays those events instead of a full snapshot. Otherwise
   (no id, gap not covered, or nothing new since that id) the server replies with a full
   snapshot — a reconnect always receives exactly one coherent reply, never silence.
4. Buffer is bounded (`backend` keeps the last N events, default 500); older gaps force
   a full snapshot.
5. Duplicate events with an id the client already applied are dropped client-side.

## Rate rules

- `/field` pings every `position.ping_live_seconds` (5 s) while duty status is active,
  every `position.ping_standby_seconds` (300 s) on standby.
- Server accepts at most one ping per member per second; excess pings are coalesced,
  newest wins.
- `member.position` is broadcast to `/ops` at most every 2 s per member (coalesced),
  so the map does not thrash.

## UI action → contract map

The prototype (`prototype/ui-prototype.html`) calls `call(method, path, body)` for REST
and `emit(type, payload)` for server events; this is the mapping it must match. Wire
format is snake_case (`vehicle_id`, `water_source_id`).

| UI action | Client → server | Server → client event |
| --- | --- | --- |
| Active incident list | `GET /incidents?status=active` | `incident.created`, `incident.updated` |
| Dispatch (Kirim) | `POST /incidents/{id}/assignments` | `assignment.updated`, `member.status` |
| Change an assignment | `PATCH /assignments/{id}` | `assignment.updated` |
| Water-source recommendation | `POST /routing/water-source-options` | — (request/response) |
| Route preview | `POST /routing/route` | — (request/response) |
| Officer changes status | `WS status.set` | `member.status` (+ `incident.updated` cascade) |
| Position reporting | `WS position.ping` | `member.position` |
| Update water level | `POST /vehicles/{id}/water-level` | `vehicle.water_level` |
| Refill finished | `POST /vehicles/{id}/refill-done` | `vehicle.water_level` |
| "This road doesn't fit" | `POST /road-restrictions` | `restriction.reported` |
| Verify / reject restriction | `POST /road-restrictions/{id}/verify` | `restriction.updated` |
| Reporter link | `POST /incidents/{id}/reporter-link` | `incident.updated` when location arrives |
| Reporter sends location | `POST /reporter/{token}/location` (no auth) | `incident.updated` |
| Top bar numbers | `GET /stats/today` | refreshed on `incident.updated` |
| Reconnect / first load | `GET /snapshot` or `WS snapshot.request` | snapshot reply |

## Versioning

Payload schema version bumps only when a field is removed or repurposed; adding a field
keeps `v` and clients must tolerate unknown keys.
