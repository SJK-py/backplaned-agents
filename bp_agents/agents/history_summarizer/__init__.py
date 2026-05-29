"""history_summarizer (group l3, hidden) — rolling summarization.

Reached only by the channel. Read-only over `session_history`: given a
thread (`agent_id`) and a cutoff (`up_to`), it folds the previous summary
+ the cutoff window into an updated summary and returns it as
`AgentOutput(content=<summary>)`. The **channel** applies the result
(writes the summary into session-info, flips `incumbent`) — the
summarizer never writes ([sessions.md] §3).
"""

from bp_agents.agents.history_summarizer.agent import (
    HISTORY_SUMMARIZER_AGENT_ID,
    NameSession,
    SummarizeAll,
    SummarizeIncumbent,
    agent,
    run_name_session,
    run_summarize_all,
    run_summarize_incumbent,
)

__all__ = [
    "HISTORY_SUMMARIZER_AGENT_ID",
    "NameSession",
    "SummarizeAll",
    "SummarizeIncumbent",
    "agent",
    "run_name_session",
    "run_summarize_all",
    "run_summarize_incumbent",
]
