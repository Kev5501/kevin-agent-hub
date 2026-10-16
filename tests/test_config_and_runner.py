from __future__ import annotations

from pathlib import Path

import pytest

from core import runner
from core.agents.incremental_extraction import load_columns
from core.config import (
    ConfigError,
    discover_agents,
    load_connections,
    load_defaults,
    resolve_env_params,
)
from core.llm import FallbackLLM
from core.llm.base import LLMError, RateLimitError, parse_json_object
from core.llm.http import post_json

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_config_is_valid():
    defaults = load_defaults(ROOT)
    connections = load_connections(ROOT)
    agents = discover_agents(ROOT, defaults)
    assert [a.name for a in agents] == ["workout_comments"]
    agent = agents[0]
    assert agent.schedule == defaults["schedule"] and agent.connection in connections
    columns = load_columns(agent.path / "columns.yaml")
    assert len(columns) >= 10
    assert len({c.name for c in columns}) == len(columns)


def test_resolve_env_params(monkeypatch):
    monkeypatch.setenv("MY_ID", "abc12345")
    assert resolve_env_params({"x_env": "MY_ID", "y": 2}) == {"x": "abc12345", "y": 2}
    monkeypatch.delenv("MY_ID")
    with pytest.raises(ConfigError, match="MY_ID"):
        resolve_env_params({"x_env": "MY_ID"})


def test_runner_list_and_dry_run_config_errors(capsys):
    assert runner.main(["--root", str(ROOT), "--list"]) == 0
    assert "workout_comments" in capsys.readouterr().out
    assert runner.main(["--root", str(ROOT), "--agent", "nope"]) == 2


def test_runner_failure_is_isolated_and_reported(monkeypatch):
    for var in ("GOOGLE_SA_JSON", "TRACKER_SHEET_ID", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # missing secrets -> agent fails cleanly (exit 1), no crash
    assert runner.main(["--root", str(ROOT), "--agent", "workout_comments", "--dry-run"]) == 1


def test_parse_json_object_variants():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(LLMError):
        parse_json_object("no json here")


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._p, self.headers, self.text = status, payload, headers or {}, "x"

    def json(self):
        return self._p


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        return self.responses.pop(0)


def test_post_json_retries_then_succeeds():
    s = _Session([_Resp(429), _Resp(503), _Resp(200, {"ok": True})])
    slept = []
    out = post_json("u", {}, {}, session=s, sleep=slept.append)
    assert out == {"ok": True} and s.calls == 3 and len(slept) == 2


def test_post_json_raises_rate_limit_after_retries():
    s = _Session([_Resp(429)] * 4)
    with pytest.raises(RateLimitError):
        post_json("u", {}, {}, session=s, sleep=lambda _: None)


def test_post_json_client_error_fails_fast():
    s = _Session([_Resp(400)])
    with pytest.raises(LLMError):
        post_json("u", {}, {}, session=s, sleep=lambda _: None)
    assert s.calls == 1


class _Stub:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc

    def generate_json(self, system, user):
        if self.exc:
            raise self.exc
        return self.result


def test_fallback_llm():
    assert FallbackLLM([_Stub(exc=RateLimitError("x")), _Stub({"ok": 1})]).generate_json(
        "", ""
    ) == {"ok": 1}
    with pytest.raises(RateLimitError):
        FallbackLLM([_Stub(exc=RateLimitError("x"))]).generate_json("", "")
    with pytest.raises(LLMError):
        FallbackLLM([_Stub(exc=LLMError("y"))]).generate_json("", "")
