"""Google Gemini (AI Studio / Developer API) client over plain REST."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from core.llm.base import LLMClient, LLMError, parse_json_object
from core.llm.http import Throttle, post_json

_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        model: str,
        min_interval_seconds: float = 12.0,
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
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        data = post_json(
            _URL.format(model=self.model),
            {"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            body,
            throttle=self.throttle,
            session=self.session,
            sleep=self._sleep,
        )
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Gemini returned no candidates (possibly blocked)") from exc
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return parse_json_object(text)
