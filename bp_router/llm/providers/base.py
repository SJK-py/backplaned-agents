"""bp_router.llm.providers.base — Provider adapter interface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from bp_router.llm.service import (
    LlmDelta,
    LlmResponse,
    Message,
    ToolChoice,
    ToolSpec,
)


class ProviderAdapter(Protocol):
    """Per-provider client; one instance per concrete model.

    Adapters translate the neutral LLM types to/from the provider's
    native API. Streaming returns an async iterator of `LlmDelta`.

    Implementations: gemini.py, anthropic.py, openai.py (to follow).
    """

    provider_name: str  # "gemini", "anthropic", "openai", ...
    concrete_model: str  # "gemini-2.5-pro", "claude-haiku-4-5", ...

    async def generate(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        provider_options: dict[str, Any] | None = None,
    ) -> LlmResponse | AsyncIterator[LlmDelta]:
        ...

    async def embed(
        self, text: str | list[str]
    ) -> list[list[float]]:
        ...

    async def count_tokens(self, messages: list[Message]) -> int:
        ...
