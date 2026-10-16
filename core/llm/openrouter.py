"""OpenRouter client (OpenAI-compatible chat completions). Useful as a free fallback provider."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from core.llm.base import LLMClient, LLMError, parse_json_object
from core.llm.http import Throttle, post_json

_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str,
        min_interval_seconds: float = 6.0,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.session = session
        self._sleep = sleep
        self.throttle = Throttle(min_interval_seconds, sleep=sleep)

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = post_json(
            _URL,
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            body,
            throttle=self.throttle,
            session=self.session,
            sleep=self._sleep,
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("OpenRouter returned no choices") from exc
        return parse_json_object(text or "")
