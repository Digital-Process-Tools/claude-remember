# Handoff between sessions (`/remember`)

Moved verbatim from the README.

Before clearing context or ending a session, type `/remember`. The agent writes a short handoff note to `.remember/remember.md` — what's done, what's next, any non-obvious context. The next session reads it and picks up where you left off. This is complementary to the automatic pipeline: the pipeline captures what happened, the handoff captures what matters next.

**The slot is not emptied on read.** Session start delivers the note and records the delivery in `tmp/remember.delivered`; the note itself stays on disk until `/remember` writes its replacement. This is deliberate — a session that never writes a handoff back (a scheduled task passing through the project, a `claude -p` one-shot, a session you abandon) used to consume the note meant for your next real session and leave nothing behind ([#221](https://github.com/Digital-Process-Tools/claude-remember/issues/221)).

The trade is that the same note can be delivered more than once. Every delivery after the first says so — *already delivered N times since ‹timestamp› — pending replacement, not news* — so a stale handoff is never mistaken for a fresh one. If you see that line, the fix is `/remember`: writing a new handoff retires the old.

An auto-compaction refire (`source=compact`) does not add to that count. It is the same session reading the same note again, not a new session seeing it for the first time, so a handoff read once and refired by four compactions still reads *delivered 1 time*, not 5 ([#341](https://github.com/Digital-Process-Tools/claude-remember/issues/341)).

**The delivery record is local to one machine and is never backed up** ([#285](https://github.com/Digital-Process-Tools/claude-remember/issues/285)). It says *this* clone has already delivered *this* handoff, and it cannot honestly say more: its timestamp is one machine's clock and its count is one machine's sessions. If you work from two machines against a shared store, the handoff itself travels — it is memory — but each machine counts its own deliveries, so a note you have already read on your laptop arrives on your desktop as news. That is the deliberate direction: being shown a note twice costs a re-read, while being told you have already acted on one you have never seen costs the work.

**Two INTERACTIVE sessions sharing one project store are a different hazard than either of the above** ([#363](https://github.com/Digital-Process-Tools/claude-remember/issues/363)). `_resolve_memory_project_dir` shares one store across a project's worktrees by design ([#56](https://github.com/Digital-Process-Tools/claude-remember/issues/56)), so two panes open on the same project are the ordinary case, not an edge one — and by default they still share one `remember.md`. If both run `/remember`, the second write silently overwrites the first, which then survives only in that session's own transcript. Set `"handoff_mode": "per_session"` to give each session its own `remember.<session_id>.md` instead; see the [Configuration](configuration.md) table. This is off by default, so an existing install keeps today's behaviour until it opts in.

