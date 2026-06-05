"""sandbox (group infra) — per-user workspace + bash execution.

v1 model (shared container, per-uid isolation): one sandbox process,
users isolated by uid + a `<sandbox_root>/<user_id>` workspace. `bash`
runs a command in the user's workspace (dropping to their uid when
configured + permitted); `stash_to_workspace` / `workspace_to_stash`
bridge the router file store and the container filesystem. All modes are
tool-visible (computer_use's LLM calls them). See [agents.md].
"""

from bp_agents.agents.sandbox.agent import (
    SANDBOX_AGENT_ID,
    Bash,
    StashToWorkspace,
    WorkspaceToStash,
    agent,
    run_bash,
)

__all__ = [
    "SANDBOX_AGENT_ID",
    "Bash",
    "StashToWorkspace",
    "WorkspaceToStash",
    "agent",
    "run_bash",
]
