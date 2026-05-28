"""bp_agents.agents.webapp — the browser channel ([webapp.md]).

A suite process: a channel agent (WS task injection + progress) plus a
FastAPI web server (login, sessions, chat, config/cron, file stash). It
reuses the transport-free `bp_agents.channel.ChannelCore` for all channel
logic, so delegation/summarization/locking stay single-sourced with the
Telegram bot.
"""
