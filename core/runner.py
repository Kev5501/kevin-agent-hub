"""CLI entry point.

python -m core.runner --list                 show agents and their schedules
python -m core.runner --due                  run agents whose schedule says "now" (cron mode)
python -m core.runner --agent NAME [NAME..]  run specific agents regardless of schedule
python -m core.runner --all                  run every enabled agent regardless of schedule
add --dry-run to any run: plan only, no LLM calls, no writes (log shows counts only)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.agents.base import get_agent_class
from core.config import (
    AgentConfig,
    ConfigError,
    discover_agents,
    load_connections,
    load_defaults,
    resolve_env_params,
)
from core.connectors.base import Connector, create_connector
from core.llm import build_llm
from core.logging_utils import redact, setup_logging
from core.models import RunStats
from core.schedule import is_due, next_fire, previous_fire

log = logging.getLogger("runner")


class Runtime:
    """Lazily builds (and caches) connectors and the LLM client."""

    def __init__(self, defaults: dict[str, Any], connections: dict[str, dict[str, Any]]) -> None:
        self.defaults = defaults
        self.connections = connections
        self._connectors: dict[str, Connector] = {}
        self._llm = None

    def connector(self, name: str) -> Connector:
        if name not in self._connectors:
            if name not in self.connections:
                raise ConfigError(f"Connection '{name}' is not defined in connections.yaml")
            params = dict(self.connections[name])
            conn_type = params.pop("type")
            self._connectors[name] = create_connector(conn_type, resolve_env_params(params))
        return self._connectors[name]

    def llm(self):
        if self._llm is None:
            self._llm = build_llm(self.defaults.get("llm", {}))
        return self._llm


def run_one(cfg: AgentConfig, runtime: Runtime, dry_run: bool) -> RunStats:
    agent_cls = get_agent_class(cfg.type)
    connector = runtime.connector(cfg.connection)
    llm = runtime.llm() if (agent_cls.needs_llm and not dry_run) else None
    return agent_cls.from_config(cfg, connector, llm).run(dry_run=dry_run)


def write_step_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner", description=__doc__)
    parser.add_argument("--root", default=".", help="repo root (default: current directory)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--due", action="store_true")
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--agent", nargs="+", metavar="NAME")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--window-hours", type=float, default=None)
    parser.add_argument("--debug", action="store_true", help="show tracebacks (local use only)")
    args = parser.parse_args(argv)

    setup_logging(args.debug)
    root = Path(args.root).resolve()
    try:
        defaults = load_defaults(root)
        connections = load_connections(root)
        agents = discover_agents(root, defaults)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    now = datetime.now(UTC)
    window = args.window_hours or float(defaults.get("dispatcher_interval_hours", 24))

    if args.list:
        for a in agents:
            state = "enabled" if a.enabled else "DISABLED"
            prev = previous_fire(a.schedule, now, a.timezone).isoformat(timespec="minutes")
            nxt = next_fire(a.schedule, now, a.timezone).isoformat(timespec="minutes")
            due = is_due(a.schedule, now, a.timezone, window)
            print(
                f"{a.name}: {state}, cron '{a.schedule}' ({a.timezone}), "
                f"last={prev}, next={nxt}, due_now={due}"
            )
        return 0

    if args.due:
        selected = [a for a in agents if a.enabled and is_due(a.schedule, now, a.timezone, window)]
        log.info("%d of %d agents are due", len(selected), len(agents))
    elif args.all:
        selected = [a for a in agents if a.enabled]
    else:
        by_name = {a.name: a for a in agents}
        unknown = [n for n in args.agent if n not in by_name]
        if unknown:
            log.error("Unknown agent(s): %s. Known: %s", ", ".join(unknown), ", ".join(by_name))
            return 2
        selected = [by_name[n] for n in args.agent]

    runtime = Runtime(defaults, connections)
    failures = 0
    summary = [
        "### Agent run" + (" (dry run)" if args.dry_run else ""),
        "",
        "| Agent | Result |",
        "|---|---|",
    ]
    for cfg in selected:
        log.info("running agent: %s", cfg.name)
        try:
            stats = run_one(cfg, runtime, args.dry_run)
        except Exception as exc:  # isolate failures: one bad agent must not block the others
            failures += 1
            msg = f"{type(exc).__name__}: {redact(str(exc))[:300]}"
            log.error("agent %s FAILED - %s", cfg.name, msg)
            if args.debug:
                log.exception("traceback")
            summary.append(f"| {cfg.name} | FAILED - {msg} |")
        else:
            log.info("agent %s OK - %s", cfg.name, stats.summary())
            summary.append(f"| {cfg.name} | {stats.summary()} |")
    write_step_summary(summary)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
