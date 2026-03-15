# Continuous Memory for Claude Code

Claude Code starts every session blank. It doesn't know what you worked on yesterday, what conventions your team follows, or what mistakes it already made. You re-explain everything, every time.

Claude Remember fixes that. It hooks into Claude Code's lifecycle — saving sessions automatically, compressing them through Haiku into layered daily summaries, and loading them back into context on the next session start. No manual prompting, no copy-pasting notes. The agent starts every session with its history already present.

The result: your Claude Code instance develops continuity. It remembers what it learned, what broke, what worked. Not perfect recall — compressed, practical memory that fits in minimal tokens.

## How it works

```
tool use → save-session.sh → extract (Python) → summarize (Haiku) → now.md
                                                                       ↓
                                                            hourly NDC compression
                                                                       ↓
                                                              today-YYYY-MM-DD.md
                                                                       ↓
                                                            daily consolidation
                                                                       ↓
                                                              recent.md + archive.md
```

Each layer compresses the one above it. Raw exchanges become one-line summaries. Daily summaries become weekly paragraphs. The result: full context in minimal tokens.

On session start, these files are automatically injected into Claude's context window via the `SessionStart` hook. The agent begins every session with its memory already loaded — no manual prompting, no "read this file" instructions. It just remembers.

## Cost

The pipeline uses Claude Haiku for summarization and compression. Haiku is the smallest, cheapest Claude model. A typical session save costs **< $0.01** — a few thousand input tokens (the session exchanges) and a few hundred output tokens (the summary). Daily compression and consolidation add a few more Haiku calls.

In practice, running this all day costs **a few cents per day**. The Anthropic API key used by the Claude CLI is the same one that powers the calls — no separate billing.

## Requirements

- Python 3.10+
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

## Hooks

The plugin registers three Claude Code hooks:

| Hook               | Script                  | Purpose                                                   |
| ------------------ | ----------------------- | --------------------------------------------------------- |
| `SessionStart`     | `session-start-hook.sh` | Loads memory files into context, recovers missed sessions |
| `UserPromptSubmit` | `user-prompt-hook.sh`   | Injects current timestamp so the agent knows the time     |
| `PostToolUse`      | `post-tool-hook.sh`     | Auto-saves session when tool call delta exceeds threshold |

Each hook sources `log.sh` for shared config, timezone, logging, and the `dispatch()` system. Hooks dispatch lifecycle events (e.g., `after_user_prompt`) to extensible listeners in `hooks.d/`.

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
| `timezone`                       | `UTC`   | Timezone for timestamps and daily file boundaries  |
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
tests/              pytest suite (122 tests, 99%+ coverage)
```

## License

Source-available. See [LICENSE](LICENSE).
Use permitted. Modification, redistribution, and resale prohibited.
