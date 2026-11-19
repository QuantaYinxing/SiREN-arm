# Seed data (dev)

One entity per JSON file, stable ids (`st1`, `t1`, `v1`, members `A`..`F`, `w1`..`w4`,
`r1`/`r2`, incidents `I-25`/`I-27`). `Store.load_seed()` is idempotent: ids that already
exist in the database are skipped, and it returns the number of rows inserted per entity kind.

The coordinates are **synthetic Garut-area data** derived from the UI prototype
(`prototype/ui-prototype.html`); they stand in for real geographic data. Real fire-brigade
data is still pending, per the open questions in `docs/siren.md`:

- §12.2 — real vehicle dimensions/weights for `vehicle_profiles.json` (the truck values
  there are plausible placeholders used to exercise Valhalla truck costing).
- §12.3 — the real list of water points (name, coordinates, type, truck access) from the
  fire brigade; `water_sources.json` currently carries invented hydrant/river/pond/tank
  locations, and each source's `access_points` is just its own location.

Statuses in the seed are self-consistent: members and vehicles are all `Standby`, and the
finished incident `I-25` has only `Completed` assignments. Enum values are the Indonesian
operational names from `contracts/config.yaml`. Timestamps are not part of the files —
`load_seed` stamps them with the store's clock.
