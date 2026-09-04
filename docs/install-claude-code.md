# Installing under Claude Code

The two-line install in the README is the whole of it for most people. This page carries the rest: why you must restart, the official Anthropic marketplace and how far behind it runs, wiring the hooks by hand, and telling which version an install actually is. Moved verbatim from the README.

## From the DPT marketplace (recommended)

We maintain our own [plugin marketplace](https://github.com/Digital-Process-Tools/claude-marketplace) so updates actually work. Add it once, then install:

```
/plugin marketplace add Digital-Process-Tools/claude-marketplace
/plugin install remember@dpt-plugins
```

To update later:

```
/plugin marketplace update
```

**Restart Claude Code after installing or enabling.** Claude Code reads hook registrations when a session starts, so a plugin enabled part-way through one has no hooks wired for the rest of it — `PostToolUse` never fires and nothing is captured, with no error anywhere ([#200](https://github.com/Digital-Process-Tools/claude-remember/issues/200)). Nothing inside a hook can detect this while it is happening, so the plugin reports it at the *next* session start instead. If capture seems to be doing nothing, run `/remember:doctor`.

## From the Anthropic Marketplace

Claude Remember is also available in the official Anthropic Marketplace. In Claude Code, type `/plugin` and search for "remember".

**Releases reach this route on the catalogue's schedule, not ours, and that schedule is not predictable from ours.** `claude-plugins-official` pins each plugin by commit sha rather than by version, and an automated PR advances that pin. Two things follow, and the second is the one that matters: the bump does not fire on a cadence we can quote, and when it fires it does not necessarily pin the newest commit. The lag is unbounded, not something our own release cadence lets you predict, and it has been observed skipping more than one tagged release in a row -- not just one.

So a release is available to a DPT-marketplace install immediately, and to an official-marketplace install whenever that catalogue gets to it. We are not going to put a number on the delay; we had one here for a day and it was already wrong the day after. Measure your own exposure instead:

```
gh api repos/anthropics/claude-plugins-official/contents/.claude-plugin/marketplace.json --jq '.content' | base64 -d | grep -A6 '"name": "remember"'
git log -1 --format='%h %ad %s' --date=short <sha>
```

The first command decodes the whole catalogue and filters it down to this plugin's own entry, which carries the pinned commit sha; take that sha and give it to the second command, which resolves it against this repo's own history to a date and a commit message. Three outcomes, and only the last two mean the catalogue is behind: the pin resolves to the commit for the release you already expect (current); the pin resolves to an older commit, and the date is your lag (behind, by however long the second command says); or either command fails, or the `grep` finds no `remember` entry at all (the pin could not be read -- treat that as unknown, not as current).

**`FORCE_AUTOUPDATE_PLUGINS=1` cannot cross that boundary,** because there is nothing stale on your side to force. Against a catalogue pinned behind the current release, `claude plugin update remember@claude-plugins-official` correctly reports the plugin as already current at the pinned version. The CLI is right and the input is old ([#264](https://github.com/Digital-Process-Tools/claude-remember/issues/264)). Waiting for the next bump works; installing from the DPT marketplace above skips the wait.

**Separately, `plugin update` can report "already at latest version" from a stale local cache** without pulling first ([#37252](https://github.com/anthropics/claude-code/issues/37252), [#38271](https://github.com/anthropics/claude-code/issues/38271)). That one is a client-side cache and is a different failure from the pin lag above, though both surface the same sentence.

## Manual install

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
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/remember/scripts/session-end-hook.sh"
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


## Check your version

Look at the `version` field in `.claude-plugin/plugin.json` — **not at the `<version>` directory name in the path below.** A cache directory is named from the version present when it was created and is never renamed, so a directory called `0.7.1` can hold a manifest saying `0.8.0`. The updater compares manifests, so the manifest is the answer and the directory name is a guess ([#204](https://github.com/Digital-Process-Tools/claude-remember/issues/204)).

The plugin location depends on your install type:

| Install type                       | Location                                                                          |
| ---------------------------------- | --------------------------------------------------------------------------------- |
| DPT marketplace (macOS/Linux)      | `~/.claude/plugins/cache/dpt-plugins/remember/<version>/`                         |
| Official marketplace (macOS/Linux) | `~/.claude/plugins/cache/claude-plugins-official/remember/<version>/`             |
| Official marketplace (Windows)     | `%USERPROFILE%\.claude\plugins\cache\claude-plugins-official\remember\<version>\` |
| Local install                      | `<your-project>/.claude/remember/`                                                |

[![The Interview](https://max.dp.tools/art/og/og-the-interview-video.jpg)](https://max.dp.tools/art/2026/03/the-interview-claude-remember.mp4)

_The Interview — an AI interviews for a job it already has but can't remember doing._

**The story behind it:** [I built a memory system I'll never remember building](https://max.dp.tools/posts/134-i-built-a-memory-system-ill-never-remember-building.php) — by Max, the AI that designed it and doesn't remember.

