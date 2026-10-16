"""In-memory table connector, used by tests and handy for local experiments."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.connectors.base import TableConnector
from core.models import Record, Table, Update


class MemoryConnector(TableConnector):
    """tables = {"Sheet1": {"columns": [...], "rows": [{col: value, ...}, ...]}}
    The locator of a record is its 1-based position in `rows`."""

    def __init__(self, tables: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.tables = tables
        self.write_calls = 0

    def _read(self, table: str) -> Table:
        data = self.tables[table]
        records = [
            Record(i, {c: row.get(c, "") for c in data["columns"]})
            for i, row in enumerate(data["rows"], start=1)
        ]
        return Table(columns=list(data["columns"]), records=records)

    def ensure_fields(self, table: str, names: Sequence[str]) -> list[str]:
        data = self.tables[table]
        created = [n for n in names if n not in data["columns"]]
        for name in created:
            data["columns"].append(name)
            for row in data["rows"]:
                row[name] = ""
        return created

    def _write(self, table: str, updates: Sequence[Update]) -> int:
        self.write_calls += 1
        rows = self.tables[table]["rows"]
        count = 0
        for upd in updates:
            for col, value in upd.values.items():
                rows[upd.locator - 1][col] = value
                count += 1
        return count

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.tables[table]["rows"]
