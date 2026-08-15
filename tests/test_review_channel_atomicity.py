"""ChannelCore multi-write sequences are atomic.

Pre-release review found `maybe_summarize`, `delegate`, and `_fold_back` each
performing several dependent writes that autocommitted independently, so a
crash between them left inconsistent state (a delegate seed with no
`delegated_to`; a cleared delegation whose recap never landed).

The session-store cutover removed the class of bug rather than the instances:
the channel no longer writes a suite database at all, and a `SessionOp` batch
IS one transaction — any refusal rolls the whole list back. What is left to
pin is that each sequence is still expressed as ONE batch, because splitting
one into two would silently reintroduce the window.
"""

from __future__ import annotations

import asyncio
import inspect

from bp_agents.channel.core import ChannelCore
from tests.fake_store import FakeChannelStore, FakeStore


def _single_batch(fn) -> int:  # type: ignore[no-untyped-def]
    """How many store round trips the function's own body makes."""
    return inspect.getsource(fn).count("self._store.ops(")


def test_delegate_seeds_and_flags_in_one_batch() -> None:
    """The seed and the routing flag commit together, or a crash leaves a
    seed nobody will be routed to — or a delegation flag with no context."""
    assert _single_batch(ChannelCore.delegate) == 1


def test_fold_back_recaps_retires_and_clears_in_one_batch() -> None:
    """Three writes — recap to the orchestrator, retire to the delegate,
    clear the flag — that are only correct together. The `StatThread` read
    that precedes them is a separate call on purpose: it feeds the payload."""
    src = inspect.getsource(ChannelCore._fold_back)
    assert src.count("self._store.ops(") == 2  # the stat read, then the batch
    apply = src[src.index("recap = "):]
    assert apply.count("self._store.ops(") == 1


def test_channel_cannot_write_a_thread_at_all() -> None:
    """The strongest form of the original fix: there is no append path from
    the channel to pair with anything. `ChannelCore` names no `AppendOp`, and
    the store refuses one from a caller with no thread of its own."""
    src = inspect.getsource(ChannelCore)
    assert "AppendOp" not in src

    from bp_protocol.frames import AppendOp
    from bp_sdk.history import SessionStoreError

    store = FakeStore()
    try:
        store.execute([AppendOp(role="user", content="x")], owner=None)
    except SessionStoreError as exc:
        assert exc.code == "denied"
    else:  # pragma: no cover - the guard is the test
        raise AssertionError("the steward surface must refuse a thread write")


class _SummDispatcher:
    async def spawn_root_for_user(self, dest, payload, *, user_id, session_id, mode=None, **kw):  # type: ignore[no-untyped-def]
        return "tsk"

    async def await_root_result(self, task_id, *, timeout_s=None, on_progress=None, **kw):  # type: ignore[no-untyped-def]
        from bp_protocol.types import AgentOutput, ResultFrame, TaskStatus

        return ResultFrame(
            agent_id="history_summarizer", trace_id="0" * 32, span_id="0" * 16,
            task_id=task_id, status=TaskStatus.SUCCEEDED, status_code=200,
            output=AgentOutput(content="summarized"),
        )


def test_delegate_rolls_back_the_whole_batch_on_a_refusal() -> None:
    """A refused op fails the batch, so the seed does not land half-applied.

    Modelled by refusing the state write: the hand-over that precedes it in
    the same batch must not survive."""
    from bp_protocol.frames import SetStateOp
    from bp_sdk.history import SessionStoreError

    async def _drive() -> None:
        store = FakeStore()
        real_execute = store.execute

        def _refuse_state(ops, *, owner):  # type: ignore[no-untyped-def]
            if any(isinstance(op, SetStateOp) for op in ops):
                raise SessionStoreError("version_conflict")
            return real_execute(ops, owner=owner)

        core = ChannelCore(
            dispatcher=_SummDispatcher(), store=FakeChannelStore(store),
            delegatable_agents=frozenset({"research"}),
        )
        store.execute = _refuse_state  # type: ignore[method-assign]
        try:
            await core.delegate("usr_a", "ses_1", "research")
        except SessionStoreError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected the refused batch to propagate")
        finally:
            store.execute = real_execute  # type: ignore[method-assign]

        assert store.handovers.get("research", []) == []
        assert "delegated_to" not in store.session_state

    asyncio.run(_drive())
