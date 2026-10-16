"""Agent interface + registry. An agent turns data from a connector into updates for it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from core.models import RunStats

if TYPE_CHECKING:
    from core.config import AgentConfig
    from core.connectors.base import Connector
    from core.llm.base import LLMClient

_AGENT_TYPES: dict[str, type[Agent]] = {}


class Agent(ABC):
    needs_llm: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def from_config(
        cls, cfg: AgentConfig, connector: Connector, llm: LLMClient | None
    ) -> Agent: ...

    @abstractmethod
    def run(self, dry_run: bool = False) -> RunStats:
        """Do the work. With dry_run=True: make no LLM calls and no writes, only report a plan."""


def register_agent(type_name: str):
    def decorator(cls: type[Agent]) -> type[Agent]:
        _AGENT_TYPES[type_name] = cls
        return cls

    return decorator


def get_agent_class(type_name: str) -> type[Agent]:
    import core.agents  # noqa: F401  (registers built-in agent types)

    if type_name not in _AGENT_TYPES:
        known = ", ".join(sorted(_AGENT_TYPES)) or "(none)"
        raise KeyError(f"Unknown agent type '{type_name}'. Known types: {known}")
    return _AGENT_TYPES[type_name]
