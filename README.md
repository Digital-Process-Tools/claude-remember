# Continuous Memory for Claude Code

![1,600+ installs from the Anthropic Marketplace](docs/marketplace-1600-installs.png)

Claude Code starts every session blank. It doesn't know what you worked on yesterday, what conventions your team follows, or what mistakes it already made. You re-explain everything, every time.

Claude Remember fixes that. It hooks into Claude Code's lifecycle — saving sessions automatically, compressing them through Haiku into layered daily summaries, and loading them back into context on the next session start. No manual prompting, no copy-pasting notes. The agent starts every session with its history already present.

The result: your Claude Code instance develops continuity. It remembers what it learned, what broke, what worked. Not perfect recall — compressed, practical memory that fits in minimal tokens.

## Install

### From our marketplace (recommended)

We maintain our own [plugin marketplace](https://github.com/Digital-Process-Tools/claude-marketplace) so updates actually work. Add it once, then install:

```
/plugin marketplace add Digital-Process-Tools/claude-marketplace
/plugin install remember@dpt-plugins
```

To update later:

```
/plugin marketplace update
```

### From the Anthropic Marketplace

Claude Remember is also available in the official Anthropic Marketplace. In Claude Code, type `/plugin` and search for "remember".

**Known issue:** The official marketplace's `plugin update` command may report "already at latest version" even when it's not — it checks a stale local cache without pulling first ([#37252](https://github.com/anthropics/claude-code/issues/37252), [#38271](https://github.com/anthropics/claude-code/issues/38271)). This is why we recommend installing from our marketplace instead.

### Check your version

Look at the `version` field in `.claude-plugin/plugin.json`. The plugin location depends on your install type:

| Install type | Location |
|---|---|
| DPT marketplace (macOS/Linux) | `~/.claude/plugins/cache/dpt-plugins/remember/<version>/` |
| Official marketplace (macOS/Linux) | `~/.claude/plugins/cache/claude-plugins-official/remember/<version>/` |
| Official marketplace (Windows) | `%USERPROFILE%\.claude\plugins\cache\claude-plugins-official\remember\<version>\` |
| Local install | `<your-project>/.claude/remember/` |

[![The Interview](https://max.dp.tools/art/og/og-the-interview-video.jpg)](https://max.dp.tools/art/2026/03/the-interview-claude-remember.mp4)

*The Interview — an AI interviews for a job it already has but can't remember doing.*

**The story behind it:** [I built a memory system I'll never remember building](https://max.dp.tools/posts/134-i-built-a-memory-system-ill-never-remember-building.php) — by Max, the AI that designed it and doesn't remember.

### Changelog

**v0.5.0** — Bug fixes, Python 3.9 support, DPT marketplace
- **DPT marketplace** — install from our own marketplace for reliable updates (`/plugin marketplace add Digital-Process-Tools/claude-marketplace`)
- **Python 3.9 support** — `from __future__ import annotations` in all pipeline modules (macOS ships 3.9 via CommandLineTools)
- **Fix: NDC subshell killed by `set -e`** (#14) — background compression no longer dies silently when `claude -p` returns non-zero
- **Fix: .gitignore created too late** (#17) — now created in `session-start-hook.sh` before any save triggers
- 186 tests (up from 162), 99% coverage

**v0.4.0** — Version tagging & marketplace update docs
- Documented known marketplace update bugs with workarounds ([#37252](https://github.com/anthropics/claude-code/issues/37252), [#38271](https://github.com/anthropics/claude-code/issues/38271))
- First release with proper git tags

**v0.3.0** — Path resolution overhaul (fixes #9, addresses #10)
- New `resolve-paths.sh` — single source of truth for all path resolution across local and marketplace installs
- Marketplace installs without `CLAUDE_PROJECT_DIR` now **fail with a clear FATAL error** instead of silently computing wrong paths
- All hooks log their resolved paths to `.remember/logs/` on every invocation
- Hook stderr captured to `.remember/logs/hook-errors.log` via hooks.json redirect
- 162 tests (up from 122), including realistic plugin simulation tests for both install layouts

**v0.2.0** — Windows compatibility, CLI v2.1.86+ support
- Fixed path slugging for Windows backslashes and colons
- Added UTF-8 encoding to all Python file operations
- Handle CLI v2+ JSON array response format in haiku.py

**v0.1.0** — Initial release

## How it works

```mermaid
flowchart TD
    A["tool use"] --> B["save-session.sh"]
    B --> C["extract (Python)"]
    C --> D["summarize (Haiku)"]
    D --> E["now.md"]
    E --> F["hourly NDC compression"]
    F --> G["today-YYYY-MM-DD.md"]
    G --> H["daily consolidation"]
    H --> I["recent.md + archive.md"]
```

Each layer compresses the one above it. Raw exchanges become one-line summaries. Daily summaries become weekly paragraphs. The result: full context in minimal tokens.

On session start, the `SessionStart` hook automatically injects into Claude's context:
- `identity.md` — who the agent is
- `remember.md` — the handoff note from the last session
- `now.md` — current session buffer
- `today-*.md` — today's compressed history
- `recent.md` — last 7 days
- `archive.md` — older history

No manual prompting, no "read this file" instructions. The agent begins every session with its memory already loaded. It just remembers.

## Cost

The pipeline uses Claude Haiku for summarization and compression. Haiku is the smallest, cheapest Claude model. A typical session save costs **< $0.01** — a few thousand input tokens (the session exchanges) and a few hundred output tokens (the summary). Daily compression and consolidation add a few more Haiku calls.

In practice, running this all day costs **a few cents per day**. The Anthropic API key used by the Claude CLI is the same one that powers the calls — no separate billing.

## Requirements

- Python 3.9+
- Claude CLI (`claude`) with Haiku access
- Bash 4+

## Setup

1. Copy `.claude/remember/` into your project's `.claude/` directory
2. Add the hooks to your `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/remember/scripts/session-start-hook.sh"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/remember/scripts/user-prompt-hook.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/remember/scripts/post-tool-hook.sh"
          }
        ]
      }
    ]
  }
}
```

3. Write your agent's identity in `.claude/remember/identity.md` (see `identity.example.md`)
4. Set **Auto-compact** to `false` in Claude Code preferences (`/config`) — auto-compact discards conversation history before the save pipeline can capture it. [Why this matters](https://max.dp.tools/posts/12-context-is-a-trap.php)
5. Enable the **status line** in Claude Code (`/statusline`) to see your current context usage — when context gets high, it's time to save and start a new session

## Hooks

The plugin registers three Claude Code hooks:

| Hook               | Script                  | Purpose                                                   |
| ------------------ | ----------------------- | --------------------------------------------------------- |
| `SessionStart`     | `session-start-hook.sh` | Loads memory files into context, recovers missed sessions |
| `UserPromptSubmit` | `user-prompt-hook.sh`   | Injects current timestamp so the agent knows the time     |
| `PostToolUse`      | `post-tool-hook.sh`     | Auto-saves session when tool call delta exceeds threshold |

Each hook sources `log.sh` for shared config, timezone, logging, and the `dispatch()` system. Hooks dispatch lifecycle events (e.g., `after_user_prompt`) to extensible listeners in `hooks.d/`.

## Handoff between sessions (`/remember`)

Before clearing context or ending a session, type `/remember`. The agent writes a short handoff note to `.remember/remember.md` — what's done, what's next, any non-obvious context. The next session reads it and picks up where you left off. This is complementary to the automatic pipeline: the pipeline captures what happened, the handoff captures what matters next.

## Data files

The pipeline writes to `.remember/` (created automatically, self-gitignored):

| File                           | Purpose                                           |
| ------------------------------ | ------------------------------------------------- |
| `.remember/now.md`             | Current session buffer                            |
| `.remember/today-*.md`         | Daily compressed summaries                        |
| `.remember/recent.md`          | Last 7 days consolidated                          |
| `.remember/archive.md`         | Older history consolidated                        |
| `.remember/logs/`              | Pipeline logs                                     |
| `.remember/tmp/`               | Lock files, cooldown markers                      |
| `.claude/remember/identity.md` | Your agent's identity and values (you write this) |

## Configuration

Copy `config.example.json` to `config.json` and adjust:

| Key                              | Default | Purpose                                            |
| -------------------------------- | ------- | -------------------------------------------------- |
| `data_dir`                       | `.remember` | Where output files are written                 |
| `cooldowns.save_seconds`         | `120`   | Minimum seconds between saves                      |
| `cooldowns.ndc_seconds`          | `3600`  | Compression interval (hourly)                      |
| `thresholds.min_human_messages`  | `3`     | Minimum messages before saving                     |
| `thresholds.delta_lines_trigger` | `50`    | Tool call output lines that trigger auto-save      |
| `features.ndc_compression`      | `true`  | Enable hourly compression of daily files           |
| `features.recovery`             | `true`  | Recover missed saves on session start              |
| `timezone`                       | *(system local)* | IANA name (e.g. `America/New_York`, `Europe/Paris`) for timestamps and daily file boundaries. Omit or leave empty to use the system clock's local zone. Set this explicitly on a VPS whose system clock is UTC. |
| `debug`                          | `false` | Verbose logging for cooldowns and locks            |

## Running tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

Integration tests (includes shell scripts and prompt validation):

```bash
bash scripts/run-tests.sh          # without Haiku
bash scripts/run-tests.sh --live   # with real Haiku call
```

## Architecture

```
pipeline/           Python core — extraction, prompts, parsing, types
  extract.py        Session JSONL → filtered exchanges
  haiku.py          Claude CLI wrapper + response parsing
  prompts.py        Template loading and substitution
  consolidate.py    Multi-day compression via Haiku
  log.py            Structured logging
  shell.py          Shell integration — prints eval-able variables
  types.py          Dataclasses for all pipeline data

prompts/            Prompt templates (txt with {{PLACEHOLDER}} substitution)
scripts/            Shell orchestration — locks, cooldowns, file I/O, backgrounding
tests/              pytest suite (186 tests, 99%+ coverage)
```

## License

Source-available. See [LICENSE](LICENSE).
Use permitted. Modification, redistribution, and resale prohibited.
