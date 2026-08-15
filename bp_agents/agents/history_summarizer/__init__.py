"""history_summarizer (group l3, hidden) — rolling summarization.

Read-only over the router's session store: given a thread (`agent_id`) and
an optional cutoff (`up_to`), it folds that thread's previous summary + the
cutoff window into an updated summary and returns it as
`AgentOutput(content=<summary>)`.

The summarizer never applies what it produces, and can't: only a thread's
owner may move its own floor, and the summary and the floor are only correct
together. So the **owner** applies it mid-turn (`common.thread.maybe_fold`),
or the **channel** uses the text as a delegation seed / hand-back recap and
applies nothing ([sessions.md] §3).
"""

from bp_agents.agents.history_summarizer.agent import (
    HISTORY_SUMMARIZER_AGENT_ID,
    NameSession,
    SummarizeThread,
    agent,
    run_name_session,
    run_summarize_thread,
)

__all__ = [
    "HISTORY_SUMMARIZER_AGENT_ID",
    "NameSession",
    "SummarizeThread",
    "agent",
    "run_name_session",
    "run_summarize_thread",
]
