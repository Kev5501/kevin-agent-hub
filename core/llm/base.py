"""LLM client interface. Agents only ever call `generate_json(system, user) -> dict`."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    pass


class RateLimitError(LLMError):
    """Quota/rate limit still exhausted after retries. Agents stop gracefully on this."""


class LLMClient(ABC):
    @abstractmethod
    def generate_json(self, system: str, user: str) -> dict[str, Any]: ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating code fences and stray prose."""
    cleaned = _FENCE.sub("", text.strip()).strip()
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LLMError("Model output was not a valid JSON object")
