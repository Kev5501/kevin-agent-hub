"""Shared test fixtures: a fake LLM and a small workout table."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from core.agents.incremental_extraction import ColumnSpec, IncrementalExtractionAgent
from core.connectors.memory import MemoryConnector
from core.llm.base import LLMClient

PROMPT = "Fields:\n{columns_block}\nOutput like {example}"

COLS = [
    ColumnSpec.from_dict({"name": "felt_strong", "type": "bool", "description": "felt strong"}),
    ColumnSpec.from_dict(
        {"name": "energy", "type": "int", "range": [1, 10], "description": "energy"}
    ),
    ColumnSpec.from_dict(
        {"name": "pump", "type": "int", "range": [1, 10], "description": "pump", "missing": "NA"}
    ),
]


def default_answer(field: str, wid: str) -> Any:
    known = {"felt_strong": True, "energy": 7, "pump": 8}
    if field in known:
        return known[field]
    return True if field.endswith("_flag") else 3  # valid for the shipped 1-5 / 1-10 scales


class FakeLLM(LLMClient):
    """Answers every requested field with a deterministic value. `override(call_no, payload)`
    may return a custom response dict (or raise) for a given call."""

    def __init__(self, override: Callable[[int, dict], Any] | None = None) -> None:
        self.calls: list[dict] = []
        self.systems: list[str] = []
        self.override = override

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        payload = json.loads(user.split("\n\n")[0])
        self.calls.append(payload)
        self.systems.append(system)
        if self.override:
            custom = self.override(len(self.calls), payload)
            if custom is not None:
                return custom
        results = {
            w["id"]: {f: default_answer(f, w["id"]) for f in payload["fields"]}
            for w in payload["workouts"]
        }
        return {"results": results}


def make_table(extra_cols: list[str] | None = None) -> dict[str, Any]:
    cols = ["Date", "Set_ID", "Gym_Day", "Exercise", "Comment", *(extra_cols or [])]
    rows: list[dict[str, Any]] = []

    def add(date: str, gym: str, comment: str, n: int) -> None:
        for i in range(n):
            rows.append(
                {
                    "Date": date,
                    "Set_ID": f"{date}-{i}",
                    "Gym_Day": gym,
                    "Exercise": "Bench",
                    "Comment": comment,
                    **{c: "" for c in (extra_cols or [])},
                }
            )

    add("2024-01-01", "Push", "felt strong, great pump", 3)
    add("2024-01-02", "Pull", "tired, bad sleep", 2)
    add("2024-01-03", "Legs", "", 4)  # no comment -> must be skipped
    return {"Sheet1": {"columns": cols, "rows": rows}}


def make_agent(
    connector: MemoryConnector,
    llm: LLMClient | None,
    columns=None,
    **overrides: Any,
) -> IncrementalExtractionAgent:
    params: dict[str, Any] = {
        "table": "Sheet1",
        "comment_column": "Comment",
        "workout_key": ["Date"],
        "columns": columns or COLS,
        "prompt_template": PROMPT,
        "context_fields": ["Gym_Day"],
        "batch_size": 8,
        "max_calls_per_run": 40,
    }
    params.update(overrides)
    return IncrementalExtractionAgent("test_agent", connector, llm, **params)
