# data-agents

Small, scheduled "agents" that enrich data in your own platforms, run free on GitHub Actions.
First agent: **workout_comments** - reads the free-text workout `Comment` in a Google Sheet and
fills 10+ numeric/binary columns, processing each unique workout comment only once.

## How it works

```
GitHub Actions (daily dispatcher) -> core.runner --due -> agents whose own cron is due
        agent  <->  connector (Google Sheets today; Notion/Airtable/DB later)  +  LLM client
```

* **Connectors** (`core/connectors/`) hold all platform-specific code behind 3 calls:
  `read_table`, `ensure_fields`, `write_updates`. Agents never import a platform SDK.
* **Agents** (`core/agents/`) hold the logic. `incremental_extraction` is generic: it fills blank
  cells from free text, grouping rows that share a key (a workout) so each comment is sent once.
* **Per-agent config** lives in `agents/<name>/` (`agent.yaml`, `columns.yaml`, `prompt.md`).
* **Connections** (`connections.yaml`) are defined once and shared. Secrets are GitHub secrets,
  referenced by env-var name only. One credential per platform, not per agent.
* **Schedules** are per agent (cron in `agent.yaml`; default weekly from `defaults.yaml`).

## Everyday tasks

| I want to... | Do this |
|---|---|
| Parse a new column | Add an entry to `agents/workout_comments/columns.yaml`, commit, run the workflow. All history is backfilled for that column only. |
| Change an agent's frequency | Set `schedule:` in its `agent.yaml` (finest granularity = daily). |
| Run something now | Actions -> "Run agents" -> Run workflow (optionally name the agent, optionally dry run). |
| Add an agent on an existing connection | Copy `agents/_template/` to `agents/<name>/`, edit the three files. No secrets/workflow changes. |
| Add a new platform | Add `core/connectors/<platform>.py` (see `gsheets.py`), register it, add an entry to `connections.yaml`, add one secret + one `env:` line in `run-agents.yml`. |
| Add a new kind of agent | Add a class in `core/agents/` with `@register_agent("my_type")`, then use `type: my_type`. |

## Safety properties

* Only blank cells are ever written; non-blank cells are never overwritten (enforced in the
  connector as well as the agent).
* Logs are public on a public repo: the runner logs counts only, never comment text, and redacts
  secret values.
* Re-running is always safe (idempotent), so late or duplicate scheduled runs are harmless.

## Local development (optional)

```
pip install -r requirements-dev.txt
ruff check . && pytest -q
python -m core.runner --list
```
Dry runs against a real sheet need the env vars in `.env.example` exported in your shell.
See the setup guide for the one-time GitHub/Google configuration.
