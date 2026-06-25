"""Embedding output-dimension plumbing.

The vector width is carried on the preset's `provider_options` and applied
by the embedding adapter: Gemini via `output_dimensionality` (a dict
`config`), OpenAI via the `dimensions` arg. These exercise the adapter
seam with a fake client (no google-genai / openai install needed).
"""

from __future__ import annotations

import asyncio

from bp_router.llm.presets import default_presets
from bp_router.llm.providers.gemini import GeminiAdapter
from bp_router.llm.providers.openai import OpenAIEmbeddingsAdapter


class _Emb:
    def __init__(self, dim: int) -> None:
        self.values = [0.0] * dim
        self.embedding = self.values  # OpenAI shape


class _GeminiModels:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def embed_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        dim = (config or {}).get("output_dimensionality", 3072)
        return type("R", (), {"embeddings": [_Emb(dim) for _ in contents]})()


class _GeminiClient:
    def __init__(self) -> None:
        self.aio = type("Aio", (), {"models": _GeminiModels()})()


def test_gemini_embed_passes_output_dimensionality() -> None:
    adapter = GeminiAdapter(concrete_model="gemini-embedding-2", api_key="x")
    adapter._client = _GeminiClient()
    vecs = asyncio.run(
        adapter.embed(["a", "b"], provider_options={"output_dimensionality": 1536})
    )
    call = adapter._client.aio.models.calls[0]
    assert call["config"] == {"output_dimensionality": 1536}
    assert all(len(v) == 1536 for v in vecs)


def test_gemini_embed_without_options_sends_no_config() -> None:
    adapter = GeminiAdapter(concrete_model="gemini-embedding-2", api_key="x")
    adapter._client = _GeminiClient()
    asyncio.run(adapter.embed("hi"))
    assert adapter._client.aio.models.calls[0]["config"] is None


class _OpenAIEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, *, input, model, **kwargs):
        self.calls.append({"input": input, "model": model, "kwargs": kwargs})
        dim = kwargs.get("dimensions", 1536)
        return type("R", (), {"data": [_Emb(dim) for _ in input]})()


class _OpenAIClient:
    def __init__(self) -> None:
        self.embeddings = _OpenAIEmbeddings()


def test_openai_embed_passes_dimensions() -> None:
    adapter = OpenAIEmbeddingsAdapter(
        concrete_model="text-embedding-3-small", api_key="x"
    )
    adapter._client = _OpenAIClient()
    asyncio.run(adapter.embed(["a"], provider_options={"dimensions": 512}))
    assert adapter._client.embeddings.calls[0]["kwargs"] == {"dimensions": 512}


def test_default_embedding_preset_does_not_pin_output_dimensionality() -> None:
    """The bundled embedding presets no longer pin `output_dimensionality`:
    the model emits its native vector width and the deployment sets the width
    instead (scripts/prod.sh asks for it, pins it on `default_embedding` in the
    generated overlay, and matches SUITE_EMBEDDING_DIM to it)."""
    presets = {p.name: p for p in default_presets()}
    assert presets["default_embedding"].default_provider_options == {}
    assert presets["gemini-embedding-2"].default_provider_options == {}
