"""The JSONC preset catalogue loader (`bp_router.llm.presets`).

The built-in catalogue moved out of a hardcoded `default_presets()` list into
a commentable `presets_catalog.jsonc`, loadable from an operator-supplied path
too. These tests pin the comment-stripping (esp. that `://` inside strings
survives), the loud-failure behaviour on a malformed catalogue, and the
custom-path round-trip.
"""

from __future__ import annotations

import pytest

from bp_router.llm.presets import (
    BUNDLED_CATALOG_PATH,
    default_presets,
    default_presets_with_overlay,
    load_catalog,
    strip_jsonc_comments,
)


def test_strip_preserves_uris_inside_strings() -> None:
    """`//` and `/* */` only count as comments OUTSIDE string literals, so
    `env://` / `https://` values are untouched."""
    src = """
    [
      // a line comment
      {"api_key_ref": "env://GEMINI_API_KEY", /* inline */ "base_url": "https://h//x"},
      {"name": "a\\"b"}  // an escaped quote then a comment
    ]
    """
    import json  # noqa: PLC0415

    data = json.loads(strip_jsonc_comments(src))
    assert data[0]["api_key_ref"] == "env://GEMINI_API_KEY"
    assert data[0]["base_url"] == "https://h//x"
    assert data[1]["name"] == 'a"b'


def test_strip_preserves_line_count() -> None:
    """Newlines survive comment stripping so a downstream JSON error still
    reports the right line."""
    src = "{\n  // c\n  /* a\n b */\n}\n"
    assert strip_jsonc_comments(src).count("\n") == src.count("\n")


def test_bundled_catalogue_loads_and_is_valid() -> None:
    from bp_router.llm.presets import (  # noqa: PLC0415
        is_valid_min_user_level,
        is_valid_preset_name,
        is_valid_provider,
    )

    presets = default_presets()
    assert len(presets) >= 28
    names = [p.name for p in presets]
    assert len(names) == len(set(names)), "duplicate preset names in catalogue"
    for p in presets:
        assert is_valid_preset_name(p.name), p.name
        assert is_valid_provider(p.provider), (p.name, p.provider)
        assert is_valid_min_user_level(p.min_user_level), (p.name, p.min_user_level)


def test_default_presets_matches_bundled_path() -> None:
    """`default_presets()` with no arg == loading the bundled file explicitly."""
    assert {p.name for p in default_presets()} == {
        p.name for p in load_catalog(BUNDLED_CATALOG_PATH)
    }


def test_custom_path_round_trip(tmp_path) -> None:
    cat = tmp_path / "custom.jsonc"
    cat.write_text(
        """
        // a deployment's own catalogue
        [
          {
            "name": "house-model",
            "provider": "openai-compatible",
            "concrete_model": "llama-3",
            "api_key_ref": "env://LOCAL_KEY",
            "base_url": "http://localhost:8000/v1",  /* vLLM */
            "min_user_level": "tier1"
          }
        ]
        """,
        encoding="utf-8",
    )
    presets = default_presets(cat)
    assert [p.name for p in presets] == ["house-model"]
    assert presets[0].base_url == "http://localhost:8000/v1"
    assert presets[0].min_user_level == "tier1"


def test_specified_fields_records_pinned_keys(tmp_path) -> None:
    """`load_catalog` records exactly the keys each JSONC entry listed in
    `Preset.specified_fields` — the boot re-sync overwrites only those columns.
    An explicit `null` still counts as present (pins the column to null)."""
    cat = tmp_path / "pin.jsonc"
    cat.write_text(
        '[{"name": "p", "provider": "openai", "concrete_model": "m", '
        '"api_key_ref": "env://K", "min_user_level": "tier1", '
        '"default_max_tokens": null}]',
        encoding="utf-8",
    )
    [p] = load_catalog(cat)
    assert p.specified_fields == frozenset({
        "name", "provider", "concrete_model", "api_key_ref",
        "min_user_level", "default_max_tokens",
    })
    # Omitted optional fields are absent (so the re-sync leaves them alone).
    assert "description" not in p.specified_fields
    assert "default_temperature" not in p.specified_fields


def test_bundled_minimal_presets_pin_only_identity_and_credential() -> None:
    """The trimmed bundled catalogue pins only model identity + credential on
    its plain presets, leaving policy fields (tier gate, sampling, description)
    operator-owned. `default` is one such minimal preset."""
    presets = {p.name: p for p in default_presets()}
    assert presets["default"].specified_fields == frozenset({
        "name", "provider", "concrete_model", "api_key_ref",
    })


def test_unknown_key_fails_loud(tmp_path) -> None:
    cat = tmp_path / "bad.jsonc"
    cat.write_text(
        '[{"name": "x", "provider": "openai", "concrete_model": "m", '
        '"api_key_ref": "env://K", "typoo": 1}]',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        load_catalog(cat)


def test_missing_required_field_fails_loud(tmp_path) -> None:
    cat = tmp_path / "bad2.jsonc"
    cat.write_text('[{"name": "x", "provider": "openai"}]', encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(cat)


def test_inline_api_key_without_api_key_ref_loads(tmp_path) -> None:
    """A preset may carry an INLINE `api_key` and omit `api_key_ref` entirely
    — `api_key_ref` defaults to "". This is the documented Example C shape
    (inline key, keyless/self-hosted endpoints) and must not crash at load."""
    cat = tmp_path / "inline.jsonc"
    cat.write_text(
        '[{"name": "default", "provider": "openai-compatible", '
        '"concrete_model": "deepseek-chat", "api_key": "sk-test", '
        '"base_url": "https://api.deepseek.com/v1"}]',
        encoding="utf-8",
    )
    presets = load_catalog(cat)
    assert len(presets) == 1
    assert presets[0].api_key_ref == ""
    assert presets[0].api_key == "sk-test"


def test_no_credential_at_all_loads(tmp_path) -> None:
    """Neither `api_key_ref` nor `api_key` — valid at load time (a keyless
    local endpoint). An empty ref resolves to empty and only fails at call
    time, mirroring the admin API where both default empty."""
    cat = tmp_path / "keyless.jsonc"
    cat.write_text(
        '[{"name": "local", "provider": "openai-compatible", '
        '"concrete_model": "llama3", "base_url": "http://localhost:8000/v1"}]',
        encoding="utf-8",
    )
    presets = load_catalog(cat)
    assert presets[0].api_key_ref == ""
    assert presets[0].api_key is None


def test_non_array_top_level_fails_loud(tmp_path) -> None:
    cat = tmp_path / "bad3.jsonc"
    cat.write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="array"):
        load_catalog(cat)


# ---------------------------------------------------------------------------
# Operator overlay merge (default_presets_with_overlay)
# ---------------------------------------------------------------------------


def test_overlay_none_is_just_the_base() -> None:
    """No overlay path → identical to the bundled base catalogue."""
    base = {p.name for p in default_presets()}
    merged = {p.name for p in default_presets_with_overlay(overlay_path=None)}
    assert merged == base


def test_overlay_missing_file_is_noop(tmp_path) -> None:
    """A pointed-at-but-absent overlay file is ignored (the compose mount is
    optional), not an error."""
    base = [p.name for p in default_presets()]
    merged = [p.name for p in default_presets_with_overlay(
        overlay_path=tmp_path / "nope.jsonc")]
    assert merged == base


def test_overlay_custom_wins_on_name_collision(tmp_path) -> None:
    """An overlay entry whose name collides with a built-in REPLACES it."""
    overlay = tmp_path / "custom.jsonc"
    overlay.write_text(
        """
        [
          {
            "name": "default",
            "provider": "openai-compatible",
            "concrete_model": "qwen3:32b",
            "api_key_ref": "env://OPENAI_API_KEY",
            "base_url": "http://ollama:11434/v1"
          }
        ]
        """,
        encoding="utf-8",
    )
    merged = {p.name: p for p in default_presets_with_overlay(overlay_path=overlay)}
    # `default` exists once, and it's the OVERLAY's version.
    assert merged["default"].provider == "openai-compatible"
    assert merged["default"].concrete_model == "qwen3:32b"
    # No duplicate `default`.
    names = [p.name for p in default_presets_with_overlay(overlay_path=overlay)]
    assert names.count("default") == 1
    # Built-ins not in the overlay survive.
    assert "claude" in merged


def test_overlay_adds_new_names(tmp_path) -> None:
    """An overlay name not in the base is appended."""
    overlay = tmp_path / "custom.jsonc"
    overlay.write_text(
        """
        [
          {
            "name": "house-local",
            "provider": "openai-compatible",
            "concrete_model": "llama-3",
            "api_key_ref": "env://LOCAL_KEY",
            "base_url": "http://localhost:8000/v1"
          }
        ]
        """,
        encoding="utf-8",
    )
    names = [p.name for p in default_presets_with_overlay(overlay_path=overlay)]
    assert "house-local" in names
    # Base names all still present.
    assert {p.name for p in default_presets()}.issubset(set(names))


def test_overlay_malformed_fails_loud(tmp_path) -> None:
    """A present-but-broken overlay is loud (so a typo'd override doesn't
    silently no-op)."""
    overlay = tmp_path / "bad.jsonc"
    overlay.write_text('[{"name": "x", "bogus_key": 1}]', encoding="utf-8")
    with pytest.raises(ValueError):
        default_presets_with_overlay(overlay_path=overlay)
