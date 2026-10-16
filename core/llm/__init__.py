"""LLM provider selection from defaults.yaml (`llm:` block), with optional fallback."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

from core.config import ConfigError
from core.llm.base import LLMClient, LLMError, RateLimitError
from core.llm.gemini import GeminiClient
from core.llm.openrouter import OpenRouterClient
from core.logging_utils import register_secret

log = logging.getLogger(__name__)

_PROVIDERS = {"gemini": GeminiClient, "openrouter": OpenRouterClient}


class FallbackLLM(LLMClient):
    """Try providers in order; move on when one errors or is rate limited."""

    def __init__(self, clients: Sequence[LLMClient]) -> None:
        self.clients = list(clients)

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        rate_limited = False
        last: LLMError | None = None
        for client in self.clients:
            try:
                return client.generate_json(system, user)
            except RateLimitError as exc:
                rate_limited, last = True, exc
                log.warning("%s rate limited; trying fallback", type(client).__name__)
            except LLMError as exc:
                last = exc
                log.warning("%s failed (%s); trying fallback", type(client).__name__, exc)
        if rate_limited:
            raise RateLimitError("All LLM providers are rate limited")
        raise last or LLMError("No LLM providers configured")


def _build_one(cfg: dict[str, Any]) -> LLMClient:
    provider = cfg.get("provider")
    if provider not in _PROVIDERS:
        raise ConfigError(f"Unknown llm provider '{provider}'. Known: {', '.join(_PROVIDERS)}")
    env_name = cfg.get("api_key_env")
    key = os.environ.get(env_name or "")
    if not key:
        raise ConfigError(f"Missing environment variable {env_name} for llm provider {provider}")
    register_secret(key)
    kwargs: dict[str, Any] = {"api_key": key, "model": cfg["model"]}
    if "min_interval_seconds" in cfg:
        kwargs["min_interval_seconds"] = float(cfg["min_interval_seconds"])
    return _PROVIDERS[provider](**kwargs)


def build_llm(llm_cfg: dict[str, Any]) -> LLMClient:
    if not llm_cfg:
        raise ConfigError("defaults.yaml needs an 'llm:' block")
    clients = [_build_one(llm_cfg)]
    fallback = llm_cfg.get("fallback")
    if fallback:
        try:
            clients.append(_build_one(fallback))
        except ConfigError as exc:
            log.warning("Fallback LLM disabled: %s", exc)
    return clients[0] if len(clients) == 1 else FallbackLLM(clients)
