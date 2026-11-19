"""SiREN backend package.

Contract is the source of truth: contracts/openapi.yaml, contracts/events.json,
contracts/db.sql, contracts/config.yaml. Modules:

- config       enum/threshold loader from contracts/config.yaml
- geo          haversine + polygon helpers
- state        status transitions (incident/member/vehicle) and cascades
- restrictions verified road restrictions -> Valhalla excludes
- routing      Valhalla client + straight-line estimate fallback, water ranking
- water        level readings and pump-based estimate decay
- store        SQLite mirror of contracts/db.sql (+ seed loader)
- texts        Indonesian event-log sentences (UI data, docs/rules.md §4)
- api          FastAPI routes
- ws           WebSocket hub (envelope per contracts/events.md)
- main         app factory / uvicorn entry
"""
