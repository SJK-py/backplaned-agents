"""`bp_agents.user_prefs` — the user's settings in the router's user scope.

Three things worth pinning, because each is a property the move depends on
rather than an implementation detail:

  * the values land in the USER scope, not the session's — that is what lets
    them outlive a conversation, and a regression would be invisible until a
    user opened a new session and found their name gone;
  * an absent or malformed key resolves to the operator default rather than
    failing, because settings are context and must never cost an answer;
  * the read degrades when the store is unreachable, for the same reason.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bp_agents.settings import SuiteSettings
from bp_agents.user_prefs import (
    PREF_KEYS,
    ConfigError,
    UserPrefs,
    coerce_config_value,
    decode_prefs,
    defaults_from,
    encode_pref,
    load_prefs,
    load_prefs_via_store,
    save_pref,
    save_prefs_via_store,
)
from tests.fake_store import FakeChannelStore, FakeHistory, FakeStore, state_value

_SETTINGS = SuiteSettings(
    database_url="postgresql://unused/unused",
    default_timezone="Asia/Seoul",
    default_language="ko",
    default_max_context_token_limit=64_000,
)


def _ctx(store: FakeStore, owner: str = "orchestrator"):  # noqa: ANN202
    return SimpleNamespace(
        user_id=store.user_id,
        session_id=store.session_id,
        history=FakeHistory(store, owner),
    )


# ---------------------------------------------------------------------------
# coercion + encoding
# ---------------------------------------------------------------------------


def test_coercion_and_rejection() -> None:
    assert coerce_config_value("verbose_default", "on") is True
    assert coerce_config_value("verbose_default", "nope") is False
    assert coerce_config_value("max_context_token_limit", "8000") == 8000
    assert coerce_config_value("full_name", "Ada") == "Ada"
    with pytest.raises(ConfigError):
        coerce_config_value("max_context_token_limit", "not-an-int")
    with pytest.raises(ConfigError):
        coerce_config_value("sandbox_uid", "1")  # not a user-scope field


def test_booleans_encode_as_words_not_python_repr() -> None:
    """`True` would be a Python detail leaking into a cross-language store."""
    assert encode_pref("verbose_default", True) == "true"
    assert encode_pref("verbose_default", False) == "false"
    assert encode_pref("max_context_token_limit", 9000) == "9000"


def test_encode_decode_round_trips_every_field() -> None:
    prefs = UserPrefs(
        full_name="Ada", timezone="Europe/London", language="en",
        verbose_default=True, custom_note="be terse",
        max_context_token_limit=9000,
    )
    state = {
        f: state_value(f, encode_pref(f, getattr(prefs, f))) for f in PREF_KEYS
    }
    assert decode_prefs(state, defaults=defaults_from(_SETTINGS)) == prefs


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------


def test_absent_keys_take_the_operator_defaults() -> None:
    """A user who has never opened Settings has NO keys — the normal case."""
    prefs = decode_prefs({}, defaults=defaults_from(_SETTINGS))
    assert prefs.timezone == "Asia/Seoul"
    assert prefs.language == "ko"
    assert prefs.max_context_token_limit == 64_000
    assert prefs.full_name == "" and prefs.custom_note == ""
    assert prefs.verbose_default is False


def test_a_malformed_value_falls_back_without_raising() -> None:
    """A bad stored value must not cost the user their turn — the field takes
    the default and the others are unaffected."""
    prefs = decode_prefs(
        {
            "max_context_token_limit": state_value("max_context_token_limit", "wat"),
            "full_name": state_value("full_name", "Ada"),
        },
        defaults=defaults_from(_SETTINGS),
    )
    assert prefs.max_context_token_limit == 64_000
    assert prefs.full_name == "Ada"


# ---------------------------------------------------------------------------
# in-task path (`ctx.history.user_scope`)
# ---------------------------------------------------------------------------


def test_load_and_save_use_the_user_scope_not_the_session() -> None:
    async def _drive() -> None:
        store = FakeStore()
        await save_pref(_ctx(store), "timezone", "Europe/Berlin")
        # User scope: cross-session. A session-scoped write here would be
        # silently lost the moment the user starts a new conversation.
        assert store.pref("timezone") == "Europe/Berlin"
        assert store.session_state == {}
        assert store.thread_state == {}

        prefs = await load_prefs(_ctx(store), _SETTINGS)
        assert prefs.timezone == "Europe/Berlin"
        assert prefs.language == "ko"  # untouched → operator default

    asyncio.run(_drive())


def test_load_prefs_degrades_to_defaults_when_the_store_refuses() -> None:
    """A settings read that 500s a turn is worse than one that runs on the
    operator defaults."""

    class _Broken(FakeHistory):
        async def _round_trip(self, ops):  # noqa: ANN001, ANN202
            raise RuntimeError("store down")

    async def _drive() -> None:
        store = FakeStore()
        ctx = SimpleNamespace(
            user_id="usr_a", session_id="ses_1",
            history=_Broken(store, "orchestrator"),
        )
        prefs = await load_prefs(ctx, _SETTINGS)
        assert prefs == defaults_from(_SETTINGS)

    asyncio.run(_drive())


def test_any_agent_in_the_session_reads_the_same_values() -> None:
    """User scope is shared, not per-agent — which is exactly why the router
    preferences it ACTS on live in their own table instead."""

    async def _drive() -> None:
        store = FakeStore()
        await save_pref(_ctx(store, "config"), "full_name", "Ada")
        seen = await load_prefs(_ctx(store, "research"), _SETTINGS)
        assert seen.full_name == "Ada"

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# steward path (webapp / chatbot gateway)
# ---------------------------------------------------------------------------


def test_steward_reads_and_writes_through_the_carrier_session() -> None:
    async def _drive() -> None:
        store = FakeStore()
        channel = FakeChannelStore(store)
        await save_prefs_via_store(
            channel, user_id="usr_a", session_id="ses_1",
            updates={"full_name": "Grace", "verbose_default": True},
        )
        assert store.pref("full_name") == "Grace"
        assert store.pref("verbose_default") == "true"
        assert store.session_state == {}

        prefs = await load_prefs_via_store(
            channel, user_id="usr_a", session_id="ses_1", settings=_SETTINGS
        )
        assert prefs.full_name == "Grace"
        assert prefs.verbose_default is True

    asyncio.run(_drive())


def test_a_multi_field_save_is_one_batch() -> None:
    """A settings form must not half-apply: a refusal mid-list would leave the
    user looking at a page reporting some of what they typed."""
    seen: list[int] = []

    class _Counting(FakeChannelStore):
        async def ops(self, *, user_id, session_id, ops, scope="session"):  # noqa: ANN001, ANN201
            seen.append(len(ops))
            return await super().ops(
                user_id=user_id, session_id=session_id, ops=ops, scope=scope
            )

    async def _drive() -> None:
        await save_prefs_via_store(
            _Counting(FakeStore()), user_id="usr_a", session_id="ses_1",
            updates={f: "x" for f in ("full_name", "timezone", "custom_note")},
        )

    asyncio.run(_drive())
    assert seen == [3], "three fields, one batch"


def test_an_empty_save_makes_no_round_trip() -> None:
    class _Never(FakeChannelStore):
        async def ops(self, **kw):  # noqa: ANN003, ANN201
            raise AssertionError("no batch should be sent")

    asyncio.run(
        save_prefs_via_store(
            _Never(FakeStore()), user_id="usr_a", session_id="ses_1", updates={}
        )
    )


def test_steward_read_degrades_when_the_router_is_unreachable() -> None:
    class _Broken(FakeChannelStore):
        async def ops(self, **kw):  # noqa: ANN003, ANN201
            raise RuntimeError("router down")

    prefs = asyncio.run(
        load_prefs_via_store(
            _Broken(FakeStore()), user_id="usr_a", session_id="ses_1",
            settings=_SETTINGS,
        )
    )
    assert prefs == defaults_from(_SETTINGS)
