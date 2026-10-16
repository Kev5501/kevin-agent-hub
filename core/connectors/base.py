"""Connector interface: the ONLY place platform-specific code lives.

An agent talks to a connector using three operations:
  read_table(table)            -> Table (columns + records, each with an opaque `locator`)
  ensure_fields(table, names)  -> create any missing columns/properties, return created names
  write_updates(table, ups)    -> write values, return number of cells written

To support a new platform (Notion, Airtable, Postgres, ...): subclass `TableConnector`, implement
`_read` and `_write` (+ `ensure_fields`), decorate with `@register("<type>")`, and add a
connection to connections.yaml. No agent code changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

from core.models import Table, Update, is_blank

_FACTORIES: dict[str, Callable[[dict[str, Any]], Connector]] = {}


class ConnectorError(Exception):
    pass


class Connector(ABC):
    @abstractmethod
    def read_table(self, table: str) -> Table: ...

    @abstractmethod
    def ensure_fields(self, table: str, names: Sequence[str]) -> list[str]: ...

    @abstractmethod
    def write_updates(self, table: str, updates: Sequence[Update]) -> int: ...


class TableConnector(Connector):
    """Adds a safety net: cells that were non-blank at the last read are NEVER overwritten."""

    def __init__(self) -> None:
        self._filled: dict[str, set[tuple[Any, str]]] = {}

    @abstractmethod
    def _read(self, table: str) -> Table: ...

    @abstractmethod
    def _write(self, table: str, updates: Sequence[Update]) -> int: ...

    def read_table(self, table: str) -> Table:
        data = self._read(table)
        self._filled[table] = {
            (rec.locator, col)
            for rec in data.records
            for col, value in rec.fields.items()
            if not is_blank(value)
        }
        return data

    def write_updates(self, table: str, updates: Sequence[Update]) -> int:
        filled = self._filled.setdefault(table, set())
        safe: list[Update] = []
        for upd in updates:
            values = {k: v for k, v in upd.values.items() if (upd.locator, k) not in filled}
            if values:
                safe.append(Update(upd.locator, values))
        if not safe:
            return 0
        written = self._write(table, safe)
        for upd in safe:
            for col in upd.values:
                filled.add((upd.locator, col))
        return written


def register(type_name: str):
    """Class decorator: make a connector available as `type: <type_name>` in connections.yaml."""

    def decorator(cls):
        _FACTORIES[type_name] = cls.from_params
        return cls

    return decorator


def create_connector(type_name: str, params: dict[str, Any]) -> Connector:
    import core.connectors  # noqa: F401  (imports built-in connectors so they self-register)

    if type_name not in _FACTORIES:
        known = ", ".join(sorted(_FACTORIES)) or "(none)"
        raise ConnectorError(f"Unknown connector type '{type_name}'. Known types: {known}")
    return _FACTORIES[type_name](params)
