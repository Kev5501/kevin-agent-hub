"""Whole pipeline through the CLI: shipped agent config + in-memory sheet + fake LLM."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from core import runner
from core.connectors import base as connector_base
from core.connectors.memory import MemoryConnector
from tests.helpers import FakeLLM

ROOT = Path(__file__).resolve().parents[1]


def _columns_of_shipped_agent():
    from core.agents.incremental_extraction import load_columns

    return [c.name for c in load_columns(ROOT / "agents/workout_comments/columns.yaml")]


def _build_repo(tmp_path: Path) -> Path:
    for name in ("defaults.yaml", "agents"):
        src = ROOT / name
        dst = tmp_path / name
        shutil.copytree(src, dst) if src.is_dir() else shutil.copy(src, dst)
    (tmp_path / "connections.yaml").write_text(
        "connections:\n  tracker_sheet:\n    type: memory_test\n"
    )
    # a second agent with its own (daily) schedule, copied from the shipped one
    second = tmp_path / "agents" / "second_agent"
    shutil.copytree(tmp_path / "agents" / "workout_comments", second)
    (second / "agent.yaml").write_text(
        (second / "agent.yaml").read_text().replace("name: workout_comments", "name: second_agent")
        + '\nschedule: "0 6 * * *"\n'
    )
    return tmp_path


def _sheet():
    rows = [
        {
            "Date": d,
            "Set_ID": f"{d}-{i}",
            "Gym_Day": "Push",
            "Comment": f"comment {d}",
        }
        for d in ("2024-01-01", "2024-01-02")
        for i in range(20)  # 20 set-rows share each workout comment
    ]
    return {"Sheet1": {"columns": ["Date", "Set_ID", "Gym_Day", "Comment"], "rows": rows}}


def test_cli_runs_agents_and_respects_schedules(tmp_path, monkeypatch):
    root = _build_repo(tmp_path)
    conn = MemoryConnector(_sheet())
    monkeypatch.setitem(connector_base._FACTORIES, "memory_test", lambda params: conn)
    llm = FakeLLM()
    monkeypatch.setattr(runner, "build_llm", lambda cfg: llm)

    # Monday 13:23 UTC: weekly (Mon 12:00) AND daily (06:00) agents are both due.
    class _Mon(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2024, 1, 1, 13, 23, tzinfo=UTC)

    monkeypatch.setattr(runner, "datetime", _Mon)
    assert runner.main(["--root", str(root), "--due"]) == 0

    names = _columns_of_shipped_agent()
    rows = conn.rows("Sheet1")
    assert all(n in conn.tables["Sheet1"]["columns"] for n in names)  # headers auto-created
    assert all(r[names[0]] != "" for r in rows)  # every one of the 40 rows filled
    # 2 unique workouts (40 rows) -> ONE LLM call for the first agent; second agent finds no work
    assert len(llm.calls) == 1
    assert len(llm.calls[0]["workouts"]) == 2

    # Tuesday: only the daily agent is due, and it has nothing pending -> no new calls.
    class _Tue(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2024, 1, 2, 13, 23, tzinfo=UTC)

    monkeypatch.setattr(runner, "datetime", _Tue)
    assert runner.main(["--root", str(root), "--due"]) == 0
    assert len(llm.calls) == 1
