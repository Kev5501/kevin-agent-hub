from __future__ import annotations

import pytest

from core.agents.incremental_extraction import ColumnSpec
from core.config import ConfigError
from core.connectors.memory import MemoryConnector
from core.llm.base import LLMError, RateLimitError
from tests.helpers import COLS, FakeLLM, make_agent, make_table

PARSED = ["felt_strong", "energy", "pump"]


def blank_table():
    return make_table(extra_cols=PARSED)


def test_backfill_uses_one_call_and_fills_every_row_of_a_workout():
    conn, llm = MemoryConnector(blank_table()), FakeLLM()
    stats = make_agent(conn, llm).run()

    assert len(llm.calls) == 1  # 2 workouts, 3 columns -> ONE call
    assert len(llm.calls[0]["workouts"]) == 2  # 5 commented rows -> 2 unique comments
    rows = conn.rows("Sheet1")
    assert {(r["felt_strong"], r["energy"], r["pump"]) for r in rows[:5]} == {(1, 7, 8)}
    assert stats.get("cells_written") == 5 * 3
    assert stats.get("workouts_without_comment") == 1


def test_workout_without_comment_is_left_blank():
    conn = MemoryConnector(blank_table())
    make_agent(conn, FakeLLM()).run()
    for row in conn.rows("Sheet1")[5:]:
        assert row["felt_strong"] == "" and row["energy"] == ""


def test_second_run_is_a_no_op():
    conn, llm = MemoryConnector(blank_table()), FakeLLM()
    make_agent(conn, llm).run()
    llm2 = FakeLLM()
    stats = make_agent(conn, llm2).run()
    assert llm2.calls == []
    assert stats.get("workouts_pending") == 0


def test_new_column_backfills_only_that_column():
    conn = MemoryConnector(blank_table())
    make_agent(conn, FakeLLM()).run()
    before = [dict(r) for r in conn.rows("Sheet1")]

    new_col = ColumnSpec.from_dict(
        {"name": "mood", "type": "int", "range": [1, 5], "description": "mood"}
    )
    llm = FakeLLM()
    stats = make_agent(conn, llm, columns=[*COLS, new_col]).run()

    assert stats.get("columns_created") == 1  # header added automatically
    assert len(llm.calls) == 1
    assert llm.calls[0]["fields"] == ["mood"]  # only the new column is requested
    for old, new in zip(before, conn.rows("Sheet1"), strict=True):
        for col in PARSED:
            assert new[col] == old[col]  # untouched
    assert conn.rows("Sheet1")[0]["mood"] == 3


def test_header_added_manually_is_backfilled():
    table = make_table(extra_cols=PARSED)
    conn = MemoryConnector(table)
    make_agent(conn, FakeLLM()).run()
    conn.ensure_fields("Sheet1", ["mood"])  # user adds a header; cells blank
    new_col = ColumnSpec.from_dict(
        {"name": "mood", "type": "int", "range": [1, 5], "description": "mood"}
    )
    llm = FakeLLM()
    stats = make_agent(conn, llm, columns=[*COLS, new_col]).run()
    assert llm.calls[0]["fields"] == ["mood"]
    assert stats.get("columns_created", 0) == 0


def test_partially_filled_workout_only_fills_blanks_and_never_overwrites():
    table = blank_table()
    table["Sheet1"]["rows"][0]["felt_strong"] = 0  # pre-existing value (a legit 0)
    table["Sheet1"]["rows"][0]["energy"] = 3
    conn = MemoryConnector(table)
    make_agent(conn, FakeLLM()).run()
    first = conn.rows("Sheet1")[0]
    assert first["felt_strong"] == 0 and first["energy"] == 3  # preserved
    assert first["pump"] == 8  # blank cell filled
    assert conn.rows("Sheet1")[1]["felt_strong"] == 1  # sibling rows filled normally


def test_invalid_answer_gets_one_corrective_retry():
    def override(call_no, payload):
        if call_no == 1:
            return {
                "results": {
                    w["id"]: {"felt_strong": True, "energy": 11, "pump": 5}
                    for w in payload["workouts"]
                }
            }
        return None

    llm = FakeLLM(override)
    conn = MemoryConnector(blank_table())
    stats = make_agent(conn, llm).run()
    assert len(llm.calls) == 2
    assert stats.get("workouts_failed") == 0
    assert conn.rows("Sheet1")[0]["energy"] == 7


def test_persistently_invalid_answer_leaves_cells_blank_for_next_run():
    bad = {"results": {"w1": {"felt_strong": True, "energy": 99, "pump": 1}}}
    llm = FakeLLM(lambda n, p: bad)
    conn = MemoryConnector(blank_table())
    stats = make_agent(conn, llm, batch_size=1).run()
    assert stats.get("workouts_failed") >= 1
    assert conn.rows("Sheet1")[0]["energy"] == ""


def test_null_means_not_mentioned_and_gets_placeholder():
    def nulls(n, p):
        return {"results": {w["id"]: {f: None for f in p["fields"]} for w in p["workouts"]}}

    conn = MemoryConnector(blank_table())
    make_agent(conn, FakeLLM(nulls)).run()
    row = conn.rows("Sheet1")[0]
    assert row["felt_strong"] == 0  # bool default
    assert row["energy"] == "NA"  # numeric default
    assert row["pump"] == "NA"  # explicit missing


def test_call_cap_stops_early_and_next_run_continues():
    conn = MemoryConnector(blank_table())
    llm = FakeLLM()
    stats = make_agent(conn, llm, batch_size=1, max_calls_per_run=1).run()
    assert len(llm.calls) == 1
    assert any("max_calls" in n for n in stats.notes)
    assert conn.rows("Sheet1")[0]["energy"] == 7 and conn.rows("Sheet1")[3]["energy"] == ""

    llm2 = FakeLLM()
    make_agent(conn, llm2, batch_size=1, max_calls_per_run=5).run()
    assert len(llm2.calls) == 1  # only the one remaining workout
    assert conn.rows("Sheet1")[3]["energy"] == 7


def test_rate_limit_keeps_earlier_progress_and_does_not_raise():
    def override(call_no, payload):
        if call_no == 2:
            raise RateLimitError("quota")
        return None

    conn = MemoryConnector(blank_table())
    stats = make_agent(conn, FakeLLM(override), batch_size=1).run()
    assert any("rate limit" in n for n in stats.notes)
    assert conn.rows("Sheet1")[0]["energy"] == 7  # first batch was saved
    assert conn.rows("Sheet1")[3]["energy"] == ""


def test_transient_llm_error_is_retried():
    def override(call_no, payload):
        if call_no == 1:
            raise LLMError("bad json")
        return None

    conn = MemoryConnector(blank_table())
    stats = make_agent(conn, FakeLLM(override)).run()
    assert stats.get("llm_errors") == 1
    assert conn.rows("Sheet1")[0]["energy"] == 7


def test_dry_run_makes_no_calls_and_no_writes():
    conn = MemoryConnector(blank_table())
    stats = make_agent(conn, None).run(dry_run=True)
    assert stats.get("planned_calls") == 1 and stats.get("workouts_pending") == 2
    assert conn.write_calls == 0
    assert conn.rows("Sheet1")[0]["energy"] == ""


def test_dry_run_with_missing_columns_does_not_create_them():
    conn = MemoryConnector(make_table())  # no parsed columns in sheet at all
    stats = make_agent(conn, None).run(dry_run=True)
    assert "energy" not in conn.tables["Sheet1"]["columns"]
    assert stats.get("columns_to_create") == 3


def test_columns_created_when_absent_from_sheet():
    conn = MemoryConnector(make_table())
    make_agent(conn, FakeLLM()).run()
    assert {"felt_strong", "energy", "pump"} <= set(conn.tables["Sheet1"]["columns"])
    assert conn.rows("Sheet1")[0]["energy"] == 7


def test_create_missing_columns_false_skips_unknown_columns():
    conn = MemoryConnector(make_table(extra_cols=["felt_strong"]))
    llm = FakeLLM()
    make_agent(conn, llm, create_missing_columns=False).run()
    assert llm.calls[0]["fields"] == ["felt_strong"]
    assert "energy" not in conn.tables["Sheet1"]["columns"]


def test_bool_format_boolean():
    conn = MemoryConnector(blank_table())
    make_agent(conn, FakeLLM(), bool_format="bool").run()
    assert conn.rows("Sheet1")[0]["felt_strong"] is True


def test_missing_required_column_raises_clear_error():
    conn = MemoryConnector(blank_table())
    with pytest.raises(ConfigError, match="Columns not found"):
        make_agent(conn, FakeLLM(), comment_column="Nope").run()


def test_comment_only_on_one_row_still_fills_whole_workout():
    table = blank_table()
    table["Sheet1"]["rows"][1]["Comment"] = ""
    table["Sheet1"]["rows"][2]["Comment"] = ""
    conn = MemoryConnector(table)
    make_agent(conn, FakeLLM()).run()
    assert [r["energy"] for r in conn.rows("Sheet1")[:3]] == [7, 7, 7]


def test_context_and_prompt_contents():
    llm = FakeLLM()
    make_agent(MemoryConnector(blank_table()), llm).run()
    assert llm.calls[0]["workouts"][0]["context"] == {"Gym_Day": "Push"}
    assert "felt_strong (boolean true/false)" in llm.systems[0]
    assert "energy (integer from 1 to 10)" in llm.systems[0]
