from __future__ import annotations

import pytest

from core.agents.incremental_extraction import ColumnSpec
from core.config import ConfigError


def spec(**kw):
    base = {"name": "c", "description": "d"}
    base.update(kw)
    return ColumnSpec.from_dict(base)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, 1), (False, 0), ("yes", 1), ("No", 0), (1, 1), (0, 0), ("true", 1)],
)
def test_bool_coercion(raw, expected):
    assert spec(type="bool").coerce(raw) == expected


def test_bool_rejects_garbage():
    with pytest.raises(ValueError):
        spec(type="bool").coerce("maybe")


def test_int_range_and_types():
    col = spec(type="int", range=[1, 10])
    assert col.coerce(7) == 7 and col.coerce("7") == 7 and col.coerce(7.0) == 7
    for bad in (0, 11, 7.5, "abc", True):
        with pytest.raises(ValueError):
            col.coerce(bad)


def test_float_and_enum():
    assert spec(type="float", range=[0, 1]).coerce("0.5") == 0.5
    enum = spec(type="enum", values=["low", "high"])
    assert enum.coerce("HIGH") == "high"
    with pytest.raises(ValueError):
        enum.coerce("medium")


@pytest.mark.parametrize("raw", [None, "", "null", "N/A", "NA", "not mentioned"])
def test_missing_tokens(raw):
    assert spec(type="int", range=[1, 5]).coerce(raw) == "NA"
    assert spec(type="bool").coerce(raw) == 0
    assert spec(type="bool").coerce(raw, "bool") is False
    assert spec(type="int", missing=-1).coerce(raw) == -1


def test_bad_definitions_rejected():
    for bad in (
        {"name": "", "type": "bool", "description": "d"},
        {"name": "x", "type": "bool", "description": ""},
        {"name": "x", "type": "weird", "description": "d"},
        {"name": "x", "type": "enum", "description": "d"},
        {"name": "x", "type": "int", "description": "d", "range": [5, 1]},
        {"name": "x", "type": "int", "description": "d", "missing": " "},
    ):
        with pytest.raises(ConfigError):
            ColumnSpec.from_dict(bad)
