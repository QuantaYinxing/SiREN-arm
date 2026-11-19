"""Event-log sentences in Indonesian (UI text, docs/rules.md §4 exception).

Event log and WS payloads carry machine fields; these strings are what `/ops` shows in the
bottom panel and what gets stored in event_log.text. Status/level words come from config,
never hard-coded here.
"""

from __future__ import annotations

from typing import Any

from .config import Config


def _status(config: Config, group: str, value: str) -> str:
    try:
        return config.label(group, value)
    except Exception:
        return value


def incident_created(config: Config, inc: dict) -> str:
    via = "dari tautan pelapor" if inc.get("reporter_link_id") else "dibuat operator"
    return f"{inc['id']} {inc['type']} — {via}"


def incident_updated(config: Config, inc: dict) -> str:
    return f"{inc['id']}: {_status(config, 'incident_status', inc['status'])}"


def dispatch_sent(config: Config, inc: dict, vehicle_name: str | None, member_names: list[str], water_name: str | None, members: int) -> str:
    veh = vehicle_name or "tanpa kendaraan"
    tail = f" · air: {water_name}" if water_name else ""
    return f"{inc['id']} dikirim · {veh} · {', '.join(member_names)} ({members} anggota){tail}"


def assignment_updated(config: Config, inc_id: str, vehicle_name: str | None, member_names: list[str], water_name: str | None) -> str:
    veh = vehicle_name or "tanpa kendaraan"
    tail = f" · air: {water_name}" if water_name else ""
    return f"{inc_id} → {veh} · {', '.join(member_names)}{tail}"


def member_status(config: Config, member: dict) -> str:
    return f"{member['name']}: {_status(config, 'member_status', member['status'])}"


def vehicle_status(config: Config, vehicle: dict) -> str:
    return f"{vehicle['name']}: {_status(config, 'vehicle_status', vehicle['status'])}"


def water_level(config: Config, vehicle_name: str, level: int, source: str) -> str:
    who = {"operator": "input operator", "officer": "input petugas", "estimate": "estimasi pompa"}.get(source, source)
    return f"{vehicle_name} {_status(config, 'water_level', level)} ({who})"


def refill_done(config: Config, vehicle_name: str) -> str:
    return f"{vehicle_name} isi ulang selesai — {config.level_label(config.max_level())}"


def restriction_reported(config: Config, reporter: str, reason: str) -> str:
    return f"{reporter}: {_status(config, 'restriction_reason', reason)} (menunggu verifikasi)"


def restriction_updated(config: Config, reason: str, status: str) -> str:
    return f"pembatasan {_status(config, 'restriction_reason', reason)} → {_status(config, 'restriction_status', status)}"


def water_source_updated(config: Config, source: dict, removed: bool = False) -> str:
    if removed:
        return f"sumber air {source['name']} dinonaktifkan"
    return f"sumber air {source['name']}: {_status(config, 'water_source_status', source['status'])}"


def config_updated(key: str, value: Any) -> str:
    return f"config {key} = {value}"


def reporter_link_created(config: Config, inc_id: str, url: str) -> str:
    return f"{inc_id} · tautan pelapor dibuat: {url}"


def reporter_location_received(config: Config, inc_id: str) -> str:
    return f"{inc_id} · lokasi pelapor masuk dari tautan"


def login(account: dict) -> str:
    return f"masuk: {account['username']} ({account['role']})"


def master_data(kind: str, action: str, name: str) -> str:
    return f"{kind} {action}: {name}"


def warn_missing_vehicle() -> str:
    return "Tidak ada kendaraan siaga — anggota berangkat tanpa kendaraan."


def warn_cross_team(count: int) -> str:
    return f"Anggota berasal dari {count} regu."


def warn_vehicle_in_use(vehicle_name: str, status_label: str) -> str:
    return f"Kendaraan {vehicle_name} sedang {status_label} — tetap dipakai?"


def warn_stale_gps(member_name: str, freshness: str, age_seconds: float | None) -> str:
    if freshness == "offline":
        return f"Posisi {member_name} belum terkirim (offline)."
    age = "belum ada data" if age_seconds is None else f"{int(age_seconds)} dtk lalu"
    return f"Posisi {member_name} {age} ({freshness})."
