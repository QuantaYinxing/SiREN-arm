"""Config loader: contracts/config.yaml is the source of truth for enums and knobs.

Nothing mutable is hard-coded (docs/siren.md D6). Enum canonical values are Indonesian
operational names; `label` is the Indonesian UI string, `gloss` the English explanation.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "contracts" / "config.yaml"


class ConfigError(Exception):
    """Raised for an unknown enum group, an unknown value, or a bad config path."""

    def __init__(self, message: str, allowed: list[Any] | None = None) -> None:
        super().__init__(message)
        self.allowed = allowed or []


def _set_nested(node: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = node
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


class Config:
    """Immutable-ish view of the config document. `replaced()` returns a new instance."""

    def __init__(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ConfigError("config must be a mapping")
        self.data = data

    @classmethod
    def load(cls, path: pathlib.Path | str = DEFAULT_PATH) -> "Config":
        text = pathlib.Path(path).read_text(encoding="utf-8")
        return cls(yaml.safe_load(text))

    def dump(self) -> dict[str, Any]:
        import copy

        return copy.deepcopy(self.data)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def replaced(self, dotted: str, value: Any) -> "Config":
        """Copy with one dot-path changed (used by PUT /config/{key})."""
        import copy

        data = copy.deepcopy(self.data)
        _set_nested(data, dotted, value)
        return Config(data)

    # ---- enum groups ----

    def enum_values(self, group: str) -> list[Any]:
        """Ordered values of an enum group (dict groups keep insertion order)."""
        node = self.get(group)
        if isinstance(node, dict):
            return list(node.keys())
        if isinstance(node, list):
            return list(node)
        raise ConfigError(f"unknown enum group {group!r}", allowed=sorted(self.data))

    def entry(self, group: str, value: Any) -> dict[str, Any]:
        node = self.get(group)
        if isinstance(node, dict):
            item = node.get(value)
            if isinstance(item, dict):
                return item
        raise ConfigError(f"{value!r} is not a valid {group}", allowed=self.enum_values(group))

    def label(self, group: str, value: Any) -> str:
        """Indonesian UI string. Groups defined as plain lists use the value itself."""
        node = self.get(group)
        if isinstance(node, list):
            if value in node:
                return str(value)
            raise ConfigError(f"{value!r} is not a valid {group}", allowed=node)
        return str(self.entry(group, value).get("label", value))

    def gloss(self, group: str, value: Any) -> str:
        return str(self.entry(group, value).get("gloss", value))

    def labels(self, group: str) -> dict[str, str]:
        return {str(v): self.label(group, v) for v in self.enum_values(group)}

    def validate(self, group: str, value: Any) -> Any:
        values = self.enum_values(group)
        if value not in values:
            raise ConfigError(f"{value!r} is not a valid {group}", allowed=values)
        return value

    # ---- transitions ----

    def transitions(self, kind: str) -> dict[str, list[str]]:
        node = self.get(f"transitions.{kind}")
        if not isinstance(node, dict):
            raise ConfigError(f"unknown transition table {kind!r}")
        return {str(k): [str(v) for v in node[k]] for k in node}

    def allowed_transitions(self, kind: str, status: str) -> list[str]:
        return list(self.transitions(kind).get(status, []))

    def can_transition(self, kind: str, current: str, target: str) -> bool:
        return target in self.allowed_transitions(kind, current)

    def next_action(self, status: str) -> str | None:
        return self.get(f"transitions.next_action.{status}")

    def next_action_label(self, status: str) -> str | None:
        return self.get(f"transitions.next_action_label.{status}")

    # ---- water levels ----

    def levels(self) -> list[int]:
        return [int(o["value"]) for o in self.get("water_level.options", [])]

    def level_label(self, level: int) -> str:
        for opt in self.get("water_level.options", []):
            if int(opt["value"]) == int(level):
                return str(opt["label"])
        raise ConfigError(f"{level!r} is not a valid water level", allowed=self.levels())

    def validate_level(self, level: int) -> int:
        levels = self.levels()
        lvl = int(level)
        if lvl not in levels:
            raise ConfigError(f"{level!r} is not a valid water level", allowed=levels)
        return lvl

    def min_level(self) -> int:
        return min(self.levels())

    def max_level(self) -> int:
        return max(self.levels())

    # ---- thresholds ----

    def seconds(self, dotted: str) -> float:
        value = self.get(dotted)
        if value is None:
            raise ConfigError(f"unknown threshold {dotted!r}")
        return float(value)

    def freshness(self, age_seconds: float | None) -> str:
        """live / stale / offline from the newest ping age (separate from duty status)."""
        if age_seconds is None:
            return "offline"
        if age_seconds > self.seconds("position.offline_after_seconds"):
            return "offline"
        if age_seconds > self.seconds("position.stale_after_seconds"):
            return "stale"
        return "live"
