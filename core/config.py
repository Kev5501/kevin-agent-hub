"""Loading of defaults.yaml, connections.yaml and agents/*/agent.yaml.

Secrets never live in YAML. Any key ending in `_env` names an environment variable; it is
resolved at runtime (in GitHub Actions that variable is filled from a repository secret).
Example:  `spreadsheet_id_env: TRACKER_SHEET_ID`  ->  param `spreadsheet_id` = $TRACKER_SHEET_ID
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core.logging_utils import register_secret
from core.schedule import validate_cron


class ConfigError(Exception):
    """Something is wrong with the repo's configuration (not a transient runtime error)."""


@dataclass
class AgentConfig:
    name: str
    type: str
    connection: str
    schedule: str
    timezone: str
    enabled: bool
    params: dict[str, Any]
    path: Path  # the agent's folder


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_defaults(root: Path) -> dict[str, Any]:
    defaults = load_yaml(root / "defaults.yaml")
    validate_cron_or_raise(defaults.get("schedule"), "defaults.yaml schedule")
    return defaults


def load_connections(root: Path) -> dict[str, dict[str, Any]]:
    raw = load_yaml(root / "connections.yaml").get("connections", {})
    if not isinstance(raw, dict):
        raise ConfigError("connections.yaml: 'connections' must be a mapping")
    for name, params in raw.items():
        if not isinstance(params, dict) or "type" not in params:
            raise ConfigError(f"connections.yaml: connection '{name}' needs a 'type'")
    return raw


def validate_cron_or_raise(expr: Any, where: str) -> None:
    try:
        validate_cron(expr)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def discover_agents(root: Path, defaults: dict[str, Any]) -> list[AgentConfig]:
    agents: list[AgentConfig] = []
    for yaml_path in sorted((root / "agents").glob("*/agent.yaml")):
        folder = yaml_path.parent
        if folder.name.startswith("_"):  # _template etc.
            continue
        raw = load_yaml(yaml_path)
        name = str(raw.get("name") or folder.name)
        for required in ("type", "connection"):
            if not raw.get(required):
                raise ConfigError(f"{yaml_path}: missing '{required}'")
        schedule = raw.get("schedule") or defaults.get("schedule")
        validate_cron_or_raise(schedule, f"{yaml_path} schedule")
        agents.append(
            AgentConfig(
                name=name,
                type=str(raw["type"]),
                connection=str(raw["connection"]),
                schedule=str(schedule),
                timezone=str(raw.get("timezone") or defaults.get("timezone") or "UTC"),
                enabled=bool(raw.get("enabled", True)),
                params=dict(raw.get("params") or {}),
                path=folder,
            )
        )
    names = [a.name for a in agents]
    if len(names) != len(set(names)):
        raise ConfigError("Duplicate agent names found under agents/")
    return agents


def resolve_env_params(params: dict[str, Any]) -> dict[str, Any]:
    """Turn `foo_env: VAR` entries into `foo: <value of $VAR>`. Raises if any are unset."""
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for key, value in params.items():
        if key.endswith("_env") and isinstance(value, str):
            env_value = os.environ.get(value)
            if not env_value:
                missing.append(value)
                continue
            register_secret(env_value)
            resolved[key[: -len("_env")]] = env_value
        else:
            resolved[key] = value
    if missing:
        raise ConfigError(
            "Missing environment variable(s): "
            + ", ".join(sorted(set(missing)))
            + " (in GitHub Actions: add them as repository secrets and pass them in the workflow)"
        )
    return resolved
