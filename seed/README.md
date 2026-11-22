# Seed data (dev)

One entity per JSON file, stable ids (`st1`, `t1`, `v2`, members `A`..`F`, `w1`.., `r1`/`r2`,
incidents `I-25`/`I-27`). `Store.load_seed()` is idempotent: ids that already
exist in the database are skipped, and it returns the number of rows inserted per entity kind.

Geography is the real coverage area (docs/next-real-maps_done.md):

- `station.json` — HQ damkar Tasikmalaya, `-7.311924330474647, 108.19915732405596`.
  The coverage bbox is station ± 12 km in `contracts/config.yaml` (`geodata.bbox`).
- `water_sources.json` — generated from OSM inside the bbox
  (`scripts/water_seed.py` over the cropped extract): fire hydrants, water bodies,
  rivers, tanks/towers. **Every entry is marked unverified**
  (`notes: "OSM placeholder, needs field survey"`); the brigade still has to confirm
  names, truck access, and capacity (§12.3).
- `vehicles.json` — trucks parked at the station; `road_restrictions.json` —
  placeholder geometries near the station, flagged for field verification.
- `vehicle_profiles.json` — truck dimensions are **ASSUMPTION placeholders**
  (width 2.5 m, height 3.2 m, length 8.0 m, gross weight 12 t): real figures are
  pending §12.2.
- Members carry no coordinates — they ride with their vehicle.

Statuses in the seed are self-consistent: members and vehicles are all `Standby`, and the
finished incident `I-25` has only `Completed` assignments. Enum values are the Indonesian
operational names from `contracts/config.yaml`. Timestamps are not part of the files —
`load_seed` stamps them with the store's clock.
