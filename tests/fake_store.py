"""An in-memory stand-in for the router's session store.

Suite tests exercise agents and the channel engine, not the store — but
almost all of them now touch it, so faking it once here beats stubbing
`ctx.history` per test with whatever shape that test happens to need.

Two surfaces over ONE `FakeStore`, matching the two the real system has:

  * `FakeHistory` — what an agent sees (`ctx.history`). It subclasses the
    real `bp_sdk.history.SessionHistory` and replaces only the wire hop, so
    `SessionBatch`, the handles, and `Turn` are the production code paths.
    What is faked is the router, not the SDK.
  * `FakeChannelStore` — what the channel sees (the steward HTTP surface),
    with the same refusals: it will not append to a thread, because the real
    endpoint has no way to.

Semantics that matter and are therefore modelled properly: ids are
session-wide and monotonic (the webapp merges threads by them), a floor
hides messages from a default read without deleting them, `AssertThread`
refuses a stale write, and `ConsumeHandovers` drains.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bp_protocol.frames import (
    AcquireLeaseOp,
    AppendOp,
    AssertThreadOp,
    ConsumeHandoversOp,
    GetStateOp,
    HandoverItem,
    HandOverOp,
    LeaseStatus,
    ListThreadsOp,
    ReadOp,
    RedactOp,
    ReleaseLeaseOp,
    RenewLeaseOp,
    SessionMessage,
    SessionOpResult,
    SetFloorOp,
    SetStateOp,
    StateValue,
    StatThreadOp,
    ThreadStat,
)
from bp_sdk.history import SessionHistory, SessionStoreError


def _now() -> datetime:
    return datetime.now(UTC)


class FakeStore:
    """One session's slice of the store, plus its session-wide bits."""

    def __init__(self, *, session_id: str = "ses_1", user_id: str = "usr_a") -> None:
        self.session_id = session_id
        self.user_id = user_id
        self._next_id = 1
        self.messages: list[SessionMessage] = []
        # (owner, thread) -> floor id
        self.floors: dict[tuple[str, str], int] = {}
        # (owner, thread) -> {key: StateValue}
        self.thread_state: dict[tuple[str, str], dict[str, StateValue]] = {}
        self.session_state: dict[str, StateValue] = {}
        # target agent -> queued items
        self.handovers: dict[str, list[HandoverItem]] = {}
        # Per-session metadata. Keyed by session id because the channel asks
        # about sessions OTHER than the one a turn rides — "is my current
        # default already on this channel?" is exactly that question.
        self.metadata_by_session: dict[str, dict[str, Any]] = {}
        self.lease_holder: str | None = None
        # Per-user namespace (`scope="user"`), which survives session purge.
        self.user_state: dict[str, StateValue] = {}

    # -- helpers a test can use directly --------------------------------

    def add(
        self,
        owner: str,
        role: str,
        content: str,
        *,
        thread: str = "",
        hidden: bool = False,
    ) -> SessionMessage:
        msg = SessionMessage(
            id=self._next_id, owner_agent_id=owner, thread_key=thread,
            role=role, content=content, hidden=hidden, created_at=_now(),
        )
        self._next_id += 1
        self.messages.append(msg)
        return msg

    def thread(self, owner: str, thread: str = "") -> list[SessionMessage]:
        """Every message on a thread, floor ignored — what a test asserts on."""
        return [
            m for m in self.messages
            if m.owner_agent_id == owner and m.thread_key == thread
        ]

    def roles(self, owner: str, thread: str = "") -> list[str]:
        return [m.role for m in self.thread(owner, thread)]

    def summary_of(self, owner: str, thread: str = "") -> str | None:
        entry = self.thread_state.get((owner, thread), {}).get("summary")
        return entry.value if entry else None

    # -- op execution ---------------------------------------------------

    def execute(self, ops: list[Any], *, owner: str | None) -> list[SessionOpResult]:
        """Apply a batch. `owner=None` is the steward (no thread of its own),
        which is what makes every write op refuse — the same way the real
        store refuses it, rather than by an allowlist here."""
        return [self._one(op, owner) for op in ops]

    def _require_owner(self, owner: str | None) -> str:
        if owner is None:
            raise SessionStoreError("denied")
        return owner

    def _one(self, op: Any, owner: str | None) -> SessionOpResult:  # noqa: PLR0911, PLR0912, C901
        if isinstance(op, AppendOp):
            me = self._require_owner(owner)
            msg = self.add(
                me, op.role, op.content, thread=op.thread_key, hidden=op.hidden
            )
            return SessionOpResult(kind="append", message_id=msg.id)

        if isinstance(op, SetFloorOp):
            me = self._require_owner(owner)
            key = (me, op.thread_key)
            if op.floor_id < self.floors.get(key, 0):
                raise SessionStoreError("floor_regression")
            self.floors[key] = op.floor_id
            return SessionOpResult(kind="set_floor")

        if isinstance(op, SetStateOp):
            if op.session_scoped:
                target = self.session_state
            else:
                me = self._require_owner(owner)
                target = self.thread_state.setdefault((me, op.thread_key), {})
            current = target.get(op.key)
            if (
                op.expected_version is not None
                and op.expected_version != (current.version if current else 0)
            ):
                raise SessionStoreError("version_conflict")
            if op.value is None:
                target.pop(op.key, None)
            else:
                target[op.key] = StateValue(
                    key=op.key, value=op.value,
                    version=(current.version if current else 0) + 1,
                )
            return SessionOpResult(
                kind="set_state", state=list(target.values())
            )

        if isinstance(op, RedactOp):
            me = self._require_owner(owner)
            for m in self.messages:
                if m.id in op.message_ids and m.owner_agent_id == me:
                    m.content = ""
                    m.redacted = True
            return SessionOpResult(kind="redact", affected=len(op.message_ids))

        if isinstance(op, HandOverOp):
            queue = self.handovers.setdefault(op.target_agent_id, [])
            item = HandoverItem(
                id=self._next_id, item_kind=op.item_kind,
                thread_key=op.thread_key, payload=op.payload, created_at=_now(),
            )
            self._next_id += 1
            queue.append(item)
            return SessionOpResult(kind="hand_over", message_id=item.id)

        if isinstance(op, ConsumeHandoversOp):
            me = self._require_owner(owner)
            queue = self.handovers.get(me, [])
            taken = [
                i for i in queue
                if op.item_kinds is None or i.item_kind in op.item_kinds
            ][: op.limit]
            self.handovers[me] = [i for i in queue if i not in taken]
            return SessionOpResult(kind="consume_handovers", items=taken)

        if isinstance(op, AssertThreadOp):
            target_owner = op.owner_agent_id or self._require_owner(owner)
            rows = self.thread(target_owner, op.thread_key)
            last = rows[-1].id if rows else 0
            if last != op.last_message_id:
                raise SessionStoreError("thread_conflict")
            return SessionOpResult(kind="assert_thread", last_message_id=last)

        if isinstance(op, ReadOp):
            target_owner = op.owner_agent_id or self._require_owner(owner)
            rows = self.thread(target_owner, op.thread_key)
            floor = (
                0 if op.include_retired
                else self.floors.get((target_owner, op.thread_key), 0)
            )
            out = [
                m for m in rows
                if m.id > floor
                and (op.roles is None or m.role in op.roles)
                and (op.include_hidden or not m.hidden)
                and (op.include_redacted or not m.redacted)
                and (op.since_id is None or m.id > op.since_id)
                and (op.before_id is None or m.id < op.before_id)
            ]
            # Newest-first under the limit, then back in ascending order —
            # the real store's truncation shape.
            out = out[-op.limit:] if op.limit else out
            return SessionOpResult(
                kind="read", messages=out,
                last_message_id=out[-1].id if out else 0,
            )

        if isinstance(op, GetStateOp):
            if op.session_scoped:
                source = self.session_state
            else:
                target_owner = op.owner_agent_id or self._require_owner(owner)
                source = self.thread_state.get((target_owner, op.thread_key), {})
            values = [
                v for k, v in source.items() if op.keys is None or k in op.keys
            ]
            return SessionOpResult(kind="get_state", state=values)

        if isinstance(op, StatThreadOp):
            target_owner = op.owner_agent_id or self._require_owner(owner)
            return SessionOpResult(
                kind="stat_thread",
                stat=self._stat(target_owner, op.thread_key),
            )

        if isinstance(op, ListThreadsOp):
            keys = sorted({
                (m.owner_agent_id, m.thread_key) for m in self.messages
                if op.owner_agent_id is None or m.owner_agent_id == op.owner_agent_id
            })
            return SessionOpResult(
                kind="list_threads",
                threads=[self._stat(o, th) for o, th in keys],
            )

        if isinstance(op, AcquireLeaseOp):
            if self.lease_holder in (None, op.holder_id):
                self.lease_holder = op.holder_id
                return SessionOpResult(
                    kind="acquire_lease",
                    lease=LeaseStatus(granted=True, ticket=1),
                )
            return SessionOpResult(
                kind="acquire_lease",
                lease=LeaseStatus(granted=False, ticket=2),
            )

        if isinstance(op, RenewLeaseOp):
            if self.lease_holder != op.holder_id:
                raise SessionStoreError("lease_lost")
            return SessionOpResult(kind="renew_lease")

        if isinstance(op, ReleaseLeaseOp):
            if self.lease_holder == op.holder_id:
                self.lease_holder = None
            return SessionOpResult(kind="release_lease")

        raise AssertionError(f"fake store: unhandled op {type(op).__name__}")

    def _stat(self, owner: str, thread: str) -> ThreadStat:
        rows = self.thread(owner, thread)
        floor = self.floors.get((owner, thread), 0)
        active = [m for m in rows if m.id > floor]
        return ThreadStat(
            owner_agent_id=owner,
            thread_key=thread,
            message_count=len(active),
            content_bytes=sum(len(m.content.encode()) for m in active),
            last_message_id=rows[-1].id if rows else 0,
            floor_id=floor,
        )


class FakeHistory(SessionHistory):
    """`ctx.history` for an agent, backed by `FakeStore`.

    Subclasses the real `SessionHistory` and overrides only `_round_trip`, so
    batching, handle resolution and the turn lease are the production code —
    the fake stops at the wire."""

    def __init__(self, store: FakeStore, owner: str, *, scope: str = "session"):
        super().__init__(ctx=None, dispatcher=None, scope=scope)  # type: ignore[arg-type]
        self._store = store
        self._owner = owner

    @property
    def user_scope(self) -> FakeHistory:
        return FakeHistory(self._store, self._owner, scope="user")

    async def _round_trip(self, ops: list[Any]) -> list[SessionOpResult]:
        return self._store.execute(ops, owner=self._owner)


class FakeChannelStore:
    """The channel's steward view — `bp_agents.channel.store.SessionStore`."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def ops(self, *, user_id, session_id, ops, scope="session"):  # noqa: ANN001, ANN201, ARG002
        return self.store.execute(ops, owner=None)

    async def messages(  # noqa: ANN201
        self, *, user_id, session_id, owner, thread="", roles=None,  # noqa: ANN001, ARG002
        include_retired=False, include_hidden=True, since_id=None,  # noqa: ANN001
        before_id=None, limit=200,  # noqa: ANN001
    ):
        res = self.store.execute(
            [ReadOp(
                owner_agent_id=owner, thread_key=thread, roles=roles,
                include_retired=include_retired, include_hidden=include_hidden,
                since_id=since_id, before_id=before_id, limit=limit,
            )],
            owner=None,
        )
        return res[0].messages or []

    async def patch_metadata(self, *, user_id, session_id, patch):  # noqa: ANN001, ANN201, ARG002
        metadata = self.store.metadata_by_session.setdefault(session_id, {})
        for key, value in patch.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        return dict(metadata)

    async def acquire_lease(  # noqa: ANN201
        self, *, user_id, session_id, holder_id, ttl_ms=300_000,  # noqa: ANN001, ARG002
    ):
        res = self.store.execute(
            [AcquireLeaseOp(holder_id=holder_id, ttl_ms=ttl_ms)], owner=None
        )
        lease = res[0].lease
        return (bool(lease and lease.granted), 0.01)

    async def release_lease(self, *, user_id, session_id, holder_id):  # noqa: ANN001, ANN201, ARG002
        self.store.execute([ReleaseLeaseOp(holder_id=holder_id)], owner=None)


class UpstreamSessionMixin:
    """The session-store half of a fake `UpstreamClient`, over a `FakeStore`.

    Webapp tests each keep their own `_Upstream` stub for the surface they
    care about (files, auth, …); this supplies the session half so they
    don't each grow a copy. Sessions the fake doesn't know 404, which is how
    the webapp's ownership guard is meant to fail.
    """

    #: Sessions this user owns. Tests mutate it to model closed/other-channel
    #: sessions; anything absent is "not yours" and 404s.
    _DEFAULT_SESSIONS = ("ses_1",)

    @property
    def store(self) -> FakeStore:
        if getattr(self, "_fake_store", None) is None:
            self._fake_store = FakeStore()
        return self._fake_store

    @property
    def sessions(self) -> dict[str, dict[str, Any]]:
        if getattr(self, "_fake_sessions", None) is None:
            self._fake_sessions: dict[str, dict[str, Any]] = {
                sid: {
                    "session_id": sid,
                    "opened_at": "2026-01-01T00:00:00+00:00",
                    "closed_at": None,
                    "metadata": {"kind": "webapp"},
                }
                for sid in self._DEFAULT_SESSIONS
            }
        return self._fake_sessions

    def _row(self, session_id: str) -> dict[str, Any]:
        from bp_agents.agents.webapp.upstream import UpstreamError

        row = self.sessions.get(session_id)
        if row is None:
            raise UpstreamError(404, "session not found")
        return row

    async def get_session(self, *, access_token: str, session_id: str):  # noqa: ANN201, ARG002
        return dict(self._row(session_id))

    async def patch_session(self, *, access_token: str, session_id: str, patch):  # noqa: ANN001, ANN201, ARG002
        row = self._row(session_id)
        for key, value in patch.items():
            if value is None:
                row["metadata"].pop(key, None)
            else:
                row["metadata"][key] = value
        return dict(row)

    async def list_sessions(self, *, access_token: str):  # noqa: ANN201, ARG002
        return [dict(r) for r in self.sessions.values()]

    async def create_session(self, *, access_token: str, metadata=None):  # noqa: ANN001, ANN201, ARG002
        sid = f"ses_{len(self.sessions) + 1}"
        self.sessions[sid] = {
            "session_id": sid,
            "opened_at": "2026-01-01T00:00:00+00:00",
            "closed_at": None,
            "metadata": dict(metadata or {}),
        }
        return dict(self.sessions[sid])

    async def session_threads(self, *, access_token: str, session_id: str):  # noqa: ANN201, ARG002
        self._row(session_id)
        res = self.store.execute([ListThreadsOp()], owner=None)
        return [t.model_dump(mode="json") for t in (res[0].threads or [])]

    async def session_messages(  # noqa: ANN201
        self, *, access_token, session_id, owner, roles=None,  # noqa: ANN001, ARG002
        include_retired=True, include_hidden=False, limit=500,  # noqa: ANN001
    ):
        self._row(session_id)
        res = self.store.execute(
            [ReadOp(
                owner_agent_id=owner, roles=roles,
                include_retired=include_retired,
                include_hidden=include_hidden, limit=limit,
            )],
            owner=None,
        )
        return [m.model_dump(mode="json") for m in (res[0].messages or [])]

    async def session_state(self, *, access_token, session_id, keys=None):  # noqa: ANN001, ANN201, ARG002
        self._row(session_id)
        res = self.store.execute(
            [GetStateOp(session_scoped=True, keys=keys)], owner=None
        )
        return {v.key: v.value for v in (res[0].state or [])}


def state_value(key: str, value: str, *, version: int = 1) -> StateValue:
    """A pre-set state entry, for a test that starts mid-conversation."""
    return StateValue(key=key, value=value, version=version)
