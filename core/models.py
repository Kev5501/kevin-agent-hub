"""Platform-neutral data types shared by connectors and agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Record:
    """One row/item from a data source.

    `locator` is opaque to agents: a sheet row number, a database primary key, a page id...
    Only the connector that produced it knows how to turn it back into a write target.
    """

    locator: Any
    fields: dict[str, Any]


@dataclass(frozen=True)
class Update:
    """Values to write into one record. Keys are field (column) names."""

    locator: Any
    values: dict[str, Any]


@dataclass
class Table:
    columns: list[str]
    records: list[Record]


def is_blank(value: Any) -> bool:
    """True for None and empty/whitespace-only strings. 0 and False are NOT blank."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


@dataclass
class RunStats:
    """Counts describing one agent run. Deliberately contains no row content (logs are public)."""

    agent: str
    counters: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def inc(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def set(self, key: str, value: int) -> None:
        self.counters[key] = value

    def get(self, key: str, default: int = 0) -> int:
        return self.counters.get(key, default)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in self.counters.items()]
        parts += [f"[{n}]" for n in self.notes]
        return " ".join(parts) if parts else "nothing to do"
