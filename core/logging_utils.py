"""Logging helpers. GitHub Actions logs are PUBLIC for public repos, so we redact aggressively
and never log row content (comments, values). Agents log counts and ids only."""

from __future__ import annotations

import json
import logging

_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a value (API key, spreadsheet id, ...) that must never appear in logs."""
    if not value or len(value) < 4:
        return
    _SECRETS.add(value)
    # If it's a JSON blob (e.g. a service-account key), also register its string values.
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return
    if isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, str) and len(v) >= 8:
                _SECRETS.add(v)


def redact(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(debug: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for noisy in ("urllib3", "google", "gspread"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
