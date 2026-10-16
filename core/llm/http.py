"""Small HTTP helper with throttling and retry/backoff, shared by the LLM providers."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from core.llm.base import LLMError, RateLimitError

log = logging.getLogger(__name__)


class Throttle:
    """Enforces a minimum gap between calls (free tiers often allow only ~5-10 requests/min)."""

    def __init__(
        self,
        min_interval: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    timeout: float = 120,
    max_retries: int = 3,
    backoff: tuple[float, ...] = (10.0, 30.0, 60.0),
    throttle: Throttle | None = None,
    session: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    http = session or requests
    last_status: int | None = None
    for attempt in range(max_retries + 1):
        if throttle:
            throttle.wait()
        try:
            resp = http.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as exc:
            last_status = None
            log.warning("LLM request failed (%s), attempt %d", type(exc).__name__, attempt + 1)
        else:
            last_status = resp.status_code
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise LLMError("LLM returned a non-JSON HTTP body") from exc
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
            log.warning("LLM HTTP %d, attempt %d", resp.status_code, attempt + 1)
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.isdigit() and attempt < max_retries:
                sleep(min(float(retry_after), 90))
                continue
        if attempt < max_retries:
            sleep(backoff[min(attempt, len(backoff) - 1)])
    if last_status == 429:
        raise RateLimitError("LLM rate/quota limit still hit after retries")
    raise LLMError(f"LLM request failed after retries (last status: {last_status})")
