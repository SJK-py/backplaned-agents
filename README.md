# backplaned-agents

backplaned-agents is a first-party, multi-user personal-assistant suite built on the Backplaned router and SDK. A single orchestrator runs the conversation and delegates to specialist agents — computer use, research/RAG, and deep reasoning — backed by per-user long-term memory (a fact graph) and a document knowledge base. On top it adds conversational sessions with rolling summarization, scheduled cron tasks, and user-facing channels (Telegram today, web next.)

Everything is per end-user: each task runs under the user’s own identity, and files, memory, sessions, and knowledge are isolated per user. Backplaned provides the transport, task lifecycle, delegation, file store, ACL, and LLM service; this repo layers the agents, conversation model, and channels on top.

## License

See [LICENSE](./LICENSE).
