"""Incremental, grouped LLM extraction agent.

Turns free-text (e.g. a workout comment) into many structured columns, cheaply:

* Pending work = blank cells in the configured parsed columns. Filled cells are never touched,
  so history is processed once. A newly added column is blank everywhere -> automatic backfill.
* Rows are grouped into "workouts" (rows sharing a key such as Date). Each unique comment is sent
  once and the result is written to every row of the group.
* All pending columns for a workout come back from ONE call, and several workouts share a call
  (`batch_size`), so the number of API calls is tiny.
* If a comment doesn't mention a column the model returns null and we write a placeholder
  (`missing`, default 0 for flags and "NA" otherwise) so the cell stops being "pending".
* Logs contain counts only (never comment text) because Actions logs are public.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, ClassVar

import yaml

from core.agents.base import Agent, register_agent
from core.config import AgentConfig, ConfigError
from core.connectors.base import Connector
from core.llm.base import LLMClient, LLMError, RateLimitError
from core.logging_utils import redact
from core.models import Record, RunStats, Table, Update, is_blank

log = logging.getLogger(__name__)

MISSING_TOKENS = {"", "null", "none", "na", "n/a", "nan", "not mentioned", "unknown"}
TRUE_TOKENS = {"true", "yes", "y", "1"}
FALSE_TOKENS = {"false", "no", "n", "0"}
VALID_TYPES = {"bool", "int", "float", "enum"}


# ---------------------------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    description: str
    range: tuple[float, float] | None = None
    values: tuple[str, ...] = ()
    missing: Any = None  # value written when the comment doesn't mention this column

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ColumnSpec:
        name = str(raw.get("name", "")).strip()
        ctype = str(raw.get("type", "")).strip().lower()
        description = str(raw.get("description", "")).strip()
        if not name or not description:
            raise ConfigError("Each column needs a 'name' and a 'description'")
        if ctype not in VALID_TYPES:
            raise ConfigError(f"Column '{name}': type must be one of {sorted(VALID_TYPES)}")
        rng = None
        if raw.get("range") is not None:
            lo, hi = raw["range"]
            if lo >= hi:
                raise ConfigError(f"Column '{name}': range must be [low, high]")
            rng = (float(lo), float(hi))
        values = tuple(str(v) for v in raw.get("values", ()))
        if ctype == "enum" and not values:
            raise ConfigError(f"Column '{name}': enum columns need 'values'")
        missing = raw.get("missing")
        if isinstance(missing, str) and missing.strip() == "":
            raise ConfigError(f"Column '{name}': 'missing' must not be blank")
        return cls(name, ctype, description, rng, values, missing)

    def default_value(self, bool_format: str = "int") -> Any:
        if self.missing is not None:
            return self.missing
        if self.type == "bool":
            return 0 if bool_format == "int" else False
        return "NA"

    def type_hint(self) -> str:
        if self.type == "bool":
            return "boolean true/false"
        if self.type == "enum":
            return "one of: " + ", ".join(self.values)
        noun = "integer" if self.type == "int" else "number"
        if self.range:
            return f"{noun} from {self.range[0]:g} to {self.range[1]:g}"
        return noun

    def coerce(self, raw: Any, bool_format: str = "int") -> Any:
        """Validate/convert a model-returned value. null/'NA'-like -> the column's default."""
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in MISSING_TOKENS):
            return self.default_value(bool_format)
        if self.type == "bool":
            flag = self._to_bool(raw)
            return int(flag) if bool_format == "int" else flag
        if self.type in ("int", "float"):
            if isinstance(raw, bool):
                raise ValueError(f"{self.name}: expected a number")
            try:
                number = float(str(raw).strip())
            except ValueError as exc:
                raise ValueError(f"{self.name}: expected a number") from exc
            if math.isnan(number):
                raise ValueError(f"{self.name}: expected a number")
            if self.range and not (self.range[0] <= number <= self.range[1]):
                lo, hi = self.range
                raise ValueError(f"{self.name}: {number:g} outside {lo:g}-{hi:g}")
            if self.type == "int":
                if not number.is_integer():
                    raise ValueError(f"{self.name}: expected a whole number")
                return int(number)
            return number
        # enum
        lookup = {v.lower(): v for v in self.values}
        key = str(raw).strip().lower()
        if key not in lookup:
            raise ValueError(f"{self.name}: must be one of {', '.join(self.values)}")
        return lookup[key]

    def _to_bool(self, raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        token = str(raw).strip().lower()
        if token in TRUE_TOKENS:
            return True
        if token in FALSE_TOKENS:
            return False
        raise ValueError(f"{self.name}: expected true/false")


def load_columns(path) -> list[ColumnSpec]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs = [ColumnSpec.from_dict(c) for c in raw.get("columns", [])]
    names = [s.name for s in specs]
    if len(names) != len(set(names)):
        raise ConfigError(f"{path}: duplicate column names")
    if not specs:
        raise ConfigError(f"{path}: no columns defined")
    return specs


# ---------------------------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------------------------
def build_system_prompt(template: str, columns: list[ColumnSpec]) -> str:
    block = "\n".join(f"- {c.name} ({c.type_hint()}): {c.description}" for c in columns)
    fields = ", ".join(f'"{c.name}": <value or null>' for c in columns)
    example = '{"results": {"w1": {' + fields + '}, "w2": {...}}}'
    return template.replace("{columns_block}", block).replace("{example}", example)


@dataclass
class _Workout:
    key: tuple[str, ...]
    records: list[Record] = field(default_factory=list)
    comment: str = ""
    context: dict[str, str] = field(default_factory=dict)
    pending: list[ColumnSpec] = field(default_factory=list)


def build_user_message(
    columns: list[ColumnSpec],
    todo: dict[str, _Workout],
    context_fields: list[str],
    problems: dict[str, list[str]],
) -> str:
    payload = {
        "fields": [c.name for c in columns],
        "workouts": [
            {
                "id": wid,
                **({"context": w.context} if context_fields else {}),
                "comment": w.comment,
            }
            for wid, w in todo.items()
        ],
    }
    text = json.dumps(payload, ensure_ascii=False)
    if problems:
        lines = [f"- {wid}: {'; '.join(errs)}" for wid, errs in problems.items()]
        text += "\n\nYour previous answer had problems for these workouts. Fix them:\n"
        text += "\n".join(lines)
    return text


# ---------------------------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------------------------
@register_agent("incremental_extraction")
class IncrementalExtractionAgent(Agent):
    needs_llm: ClassVar[bool] = True

    def __init__(
        self,
        name: str,
        connector: Connector,
        llm: LLMClient | None,
        *,
        table: str,
        comment_column: str,
        workout_key: list[str],
        columns: list[ColumnSpec],
        prompt_template: str,
        context_fields: list[str] | None = None,
        batch_size: int = 8,
        max_calls_per_run: int = 40,
        create_missing_columns: bool = True,
        bool_format: str = "int",
    ) -> None:
        if batch_size < 1 or max_calls_per_run < 1:
            raise ConfigError("batch_size and max_calls_per_run must be >= 1")
        if bool_format not in ("int", "bool"):
            raise ConfigError("bool_format must be 'int' (1/0) or 'bool' (TRUE/FALSE)")
        if not workout_key:
            raise ConfigError("workout_key needs at least one column")
        self.name = name
        self.connector = connector
        self.llm = llm
        self.table = table
        self.comment_column = comment_column
        self.workout_key = workout_key
        self.columns = columns
        self.prompt_template = prompt_template
        self.context_fields = context_fields or []
        self.batch_size = batch_size
        self.max_calls = max_calls_per_run
        self.create_missing_columns = create_missing_columns
        self.bool_format = bool_format

    @classmethod
    def from_config(
        cls, cfg: AgentConfig, connector: Connector, llm: LLMClient | None
    ) -> IncrementalExtractionAgent:
        p = cfg.params
        for required in ("table", "comment_column", "workout_key"):
            if required not in p:
                raise ConfigError(f"Agent '{cfg.name}': params.{required} is required")
        key = p["workout_key"]
        prompt_path = cfg.path / p.get("prompt_file", "prompt.md")
        if not prompt_path.exists():
            raise ConfigError(f"Agent '{cfg.name}': missing {prompt_path.name}")
        return cls(
            cfg.name,
            connector,
            llm,
            table=str(p["table"]),
            comment_column=str(p["comment_column"]),
            workout_key=[key] if isinstance(key, str) else list(key),
            columns=load_columns(cfg.path / p.get("columns_file", "columns.yaml")),
            prompt_template=prompt_path.read_text(encoding="utf-8"),
            context_fields=list(p.get("context_fields") or []),
            batch_size=int(p.get("batch_size", 8)),
            max_calls_per_run=int(p.get("max_calls_per_run", 40)),
            create_missing_columns=bool(p.get("create_missing_columns", True)),
            bool_format=str(p.get("bool_format", "int")),
        )

    # -- main flow --------------------------------------------------------------------------
    def run(self, dry_run: bool = False) -> RunStats:
        stats = RunStats(self.name)
        table = self.connector.read_table(self.table)
        columns, table = self._prepare_columns(table, stats, dry_run)
        self._check_required_columns(table)

        workouts = self._group(table, columns, stats)
        pending = [w for w in workouts if w.pending]
        stats.set("workouts_pending", len(pending))
        buckets = self._bucket(pending)
        planned = sum(math.ceil(len(ws) / self.batch_size) for ws in buckets.values())
        stats.set("planned_calls", planned)

        if dry_run or not pending:
            return stats
        if self.llm is None:
            raise ConfigError("This agent needs an LLM client")

        for workouts_in_bucket in buckets.values():
            for i in range(0, len(workouts_in_bucket), self.batch_size):
                batch = workouts_in_bucket[i : i + self.batch_size]
                if stats.get("llm_calls") >= self.max_calls:
                    stats.note("stopped early: max_calls_per_run reached")
                    return stats
                done, rate_limited = self._extract(batch, stats)
                self._write(batch, done, stats)
                if rate_limited:
                    stats.note("stopped early: LLM rate limit")
                    return stats
        return stats

    # -- steps ------------------------------------------------------------------------------
    def _prepare_columns(
        self, table: Table, stats: RunStats, dry_run: bool
    ) -> tuple[list[ColumnSpec], Table]:
        missing = [c for c in self.columns if c.name not in table.columns]
        if not missing:
            return list(self.columns), table
        if not self.create_missing_columns:
            log.warning("Columns not in sheet and create_missing_columns=false: %d", len(missing))
            stats.set("columns_skipped", len(missing))
            return [c for c in self.columns if c.name in table.columns], table
        stats.set("columns_to_create", len(missing))
        if dry_run:
            return list(self.columns), table  # treated as blank columns for planning
        created = self.connector.ensure_fields(self.table, [c.name for c in missing])
        stats.set("columns_created", len(created))
        return list(self.columns), self.connector.read_table(self.table)

    def _check_required_columns(self, table: Table) -> None:
        needed = [self.comment_column, *self.workout_key, *self.context_fields]
        absent = [c for c in needed if c not in table.columns]
        if absent:
            raise ConfigError(f"Columns not found in '{self.table}': {', '.join(absent)}")

    def _group(self, table: Table, columns: list[ColumnSpec], stats: RunStats) -> list[_Workout]:
        groups: dict[tuple[str, ...], _Workout] = {}
        rows_without_key = 0
        for rec in table.records:
            key = tuple(str(rec.fields.get(k, "")).strip() for k in self.workout_key)
            if any(part == "" for part in key):
                rows_without_key += 1
                continue
            group = groups.setdefault(key, _Workout(key))
            group.records.append(rec)

        stats.set("rows_total", len(table.records))
        stats.set("workouts_total", len(groups))
        if rows_without_key:
            stats.set("rows_without_key", rows_without_key)

        usable: list[_Workout] = []
        without_comment = 0
        pending_cells = 0
        for group in groups.values():
            comment = next(
                (
                    str(r.fields[self.comment_column]).strip()
                    for r in group.records
                    if not is_blank(r.fields.get(self.comment_column))
                ),
                "",
            )
            if not comment:
                without_comment += 1
                continue
            group.comment = comment
            first = group.records[0].fields
            group.context = {f: str(first.get(f, "")).strip() for f in self.context_fields}
            group.pending = [
                c for c in columns if any(is_blank(r.fields.get(c.name)) for r in group.records)
            ]
            pending_cells += sum(
                1 for r in group.records for c in columns if is_blank(r.fields.get(c.name))
            )
            usable.append(group)
        stats.set("workouts_without_comment", without_comment)
        stats.set("cells_pending", pending_cells)
        return usable

    @staticmethod
    def _bucket(pending: list[_Workout]) -> dict[tuple[str, ...], list[_Workout]]:
        """Group workouts needing the same set of columns so each call asks for one shared set
        (e.g. a newly added column only ever asks for that one column)."""
        buckets: dict[tuple[str, ...], list[_Workout]] = {}
        for w in pending:
            buckets.setdefault(tuple(c.name for c in w.pending), []).append(w)
        return buckets

    def _extract(
        self, batch: list[_Workout], stats: RunStats
    ) -> tuple[dict[int, dict[str, Any]], bool]:
        """Returns ({index_in_batch: {column: value}}, rate_limited)."""
        assert self.llm is not None
        cols = batch[0].pending
        system = build_system_prompt(self.prompt_template, cols)
        todo = {f"w{i + 1}": w for i, w in enumerate(batch)}
        done: dict[int, dict[str, Any]] = {}
        problems: dict[str, list[str]] = {}
        rate_limited = False

        for _attempt in range(2):  # first try + one corrective retry
            if not todo or stats.get("llm_calls") >= self.max_calls:
                break
            stats.inc("llm_calls")
            user = build_user_message(cols, todo, self.context_fields, problems)
            try:
                data = self.llm.generate_json(system, user)
            except RateLimitError:
                rate_limited = True
                break
            except LLMError as exc:
                stats.inc("llm_errors")
                log.warning("LLM call failed: %s", redact(str(exc))[:200])
                problems = {}
                continue

            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, dict):
                problems = {wid: ["response must contain a 'results' object"] for wid in todo}
                continue
            problems = {}
            for wid in list(todo):
                parsed, errors = self._validate(cols, results.get(wid))
                if errors:
                    problems[wid] = errors
                else:
                    done[int(wid[1:]) - 1] = parsed
                    del todo[wid]

        if todo:
            stats.inc("workouts_failed", len(todo))
        return done, rate_limited

    def _validate(self, cols: list[ColumnSpec], entry: Any) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(entry, dict):
            return {}, ["missing from response"]
        parsed: dict[str, Any] = {}
        errors: list[str] = []
        for col in cols:
            if col.name not in entry:
                errors.append(f"{col.name}: field missing (use null if not mentioned)")
                continue
            try:
                parsed[col.name] = col.coerce(entry[col.name], self.bool_format)
            except ValueError as exc:
                errors.append(str(exc))
        return parsed, errors

    def _write(
        self, batch: list[_Workout], done: dict[int, dict[str, Any]], stats: RunStats
    ) -> None:
        updates: list[Update] = []
        for index, values in done.items():
            for rec in batch[index].records:
                to_write = {c: v for c, v in values.items() if is_blank(rec.fields.get(c))}
                if to_write:
                    updates.append(Update(rec.locator, to_write))
        if updates:
            stats.inc("cells_written", self.connector.write_updates(self.table, updates))
        stats.inc("workouts_filled", len(done))
        log.info(
            "batch done: %d/%d workouts filled, %d cells written so far",
            len(done),
            len(batch),
            stats.get("cells_written"),
        )
