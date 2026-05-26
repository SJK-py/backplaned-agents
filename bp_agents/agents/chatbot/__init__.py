"""chatbot (group channel, hidden) — Telegram channel + session manager.

A long-running **gateway** (not a normal handler-agent): `on_startup`
launches the Telegram long-poll loop, each inbound message is injected as
a root task on behalf of the user (`spawn_root_for_user`), and the normal
reply is the awaited result. Owns the per-session queue and all
session-info / user-turn writes ([overview.md] §2.3, [channel.md]).

Phase 1 ships the message round-trip (identity resolution → dispatch →
relay) + `/help`. Registration/approval, per-user credentials, and the
`/new` `/stop` `/register` commands land in the next slice.
"""
