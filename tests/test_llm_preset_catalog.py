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


def test_non_array_top_level_fails_loud(tmp_path) -> None:
    cat = tmp_path / "bad3.jsonc"
    cat.write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="array"):
        load_catalog(cat)
