# Installing under Antigravity CLI (agy)

Observed against `agy` 1.1.27 (macOS darwin/arm64), not only reasoned from the docs -- every claim below that is not marked REASONED was driven end to end against a real `agy` process, in the same session that wrote this port (#563, building on #553's own investigation).

## Antigravity has no per-plugin manifest

`agy plugin install` copies a plugin's own `hooks.json` into `~/.gemini/config/plugins/<name>/`, counts it (`✔ hooks : 1 processed`), marks the plugin `enabled: true` -- and never loads it (#553). The only two paths `agy` actually loads hooks from are the shared `~/.gemini/config/hooks.json` (every plugin on the machine) and the untested workspace-local `<workspace>/.agents/hooks.json`. This repo does not ship a static manifest for either path, unlike `hooks/hooks.codex.json` or `.gemini/hooks/hooks.json`: neither path supports the kind of variable this repo could check into git and have resolve on another machine (see below), so there is nothing stable to check in. Install with:

```
python3 scripts/install_agy_hooks.py
```

which merges a "remember" entry into `~/.gemini/config/hooks.json`, preserving every other top-level hook name already there -- the file is shared, not this plugin's alone. `--target PATH` overrides the location (mainly for testing); `--dry-run` prints the merged document without writing it. If the existing file cannot be parsed as a JSON object at all (a hand edit gone wrong, a half-written file from a concurrent process), the installer refuses and writes nothing rather than treating "cannot parse" as "nothing here" and silently discarding whatever other plugin's entries were in it.

## The schema is name-keyed, and no variable resolves inside it

`agy` 1.1.27 accepts a hook name mapping to an object keyed by event, each holding an array of handler objects:

```json
{
  "remember": {
    "enabled": true,
    "SessionStart": [ { "type": "command", "command": "bash /abs/path/scripts/agy-session-start-hook.sh", "timeout": 30 } ]
  }
}
```

Claude Code's own shape -- an event key holding an array of matcher objects -- does not parse at all, and the failure is invisible: `agy -p "/hooks" --output-format json` answers `{"hooks":[]}`, byte-identical to having no file; the only witness is `~/.gemini/antigravity-cli/cli.log` (`hooks_manager.go`'s `failed to parse hooks.json` / `loaded 0 named hooks`). See `.claude/jit-context/vocabulary/02-hosts/antigravity-hooks.md` for the full trap.

**Every command is a literal, already-resolved absolute path.** Nothing observed on Antigravity sets a plugin-root or project-dir environment variable (`pipeline/host.py`'s `ANTIGRAVITY` host declares neither, confirmed by dumping a live hook process's own environment -- `tests/fixtures/antigravity-env-563.txt`), and Gemini CLI's own `${extensionPath}` substitution is documented as extension-local only, which the shared, non-extension `~/.gemini/config/hooks.json` is not. `scripts/install_agy_hooks.py` resolves the real path at install time instead of shipping a manifest with a placeholder that would never expand.

## The mapping (re-derived against 1.1.27, corrects #553's own earlier framing)

| Remember captures on | Antigravity analogue | wired to |
| --- | --- | --- |
| `SessionStart` | `SessionStart` (same name; carries `transcriptPath` at session open) | `scripts/agy-session-start-hook.sh` -> `session-start-hook.sh` |
| `UserPromptSubmit` | `PreInvocation` (fires per model invocation, **not** per user prompt) | `scripts/agy-pre-invocation-hook.sh` -> `user-prompt-hook.sh` |
| `Stop` | `Stop` (a **turn boundary**, not a teardown -- see below) | `scripts/agy-stop-hook.sh` (does NOT delegate to `session-end-hook.sh`) |
| `SessionEnd` | **no analogue found** -- see below | not wired |

`PostInvocation` is confirmed to load and fire too, but nothing in this plugin has a use for it (no `PostToolUse`-shaped work to do with it), so it is deliberately left out of the installed manifest.

Every adapter script renames Antigravity's own field names (`conversationId`, `transcriptPath`, `workspacePaths`) into the shape the delegate script already understands (`session_id`, `transcript_path`, `cwd`) rather than teaching the delegate scripts a second payload shape -- the same "existing scripts only" discipline #410 held the Codex manifest to, one layer removed: here the SHAPE differs, not just the event name, so a thin renaming adapter was unavoidable.

## `Stop` fires after every turn -- it is not wired to `session-end-hook.sh`

manaflow-ai/cmux#5000 (cited in #563) is the expensive way to learn this: treating a restorable agent's turn-end as a session end destroyed their restore record after the first turn. `session-end-hook.sh` is built to run exactly once, at the actual end of a session, and does backup/consolidation work on that assumption -- repeating it on every turn boundary would be wrong in the same way. `scripts/agy-stop-hook.sh` does not source or call it.

Instead it does what `post-tool-hook.sh` already does safely many times a session: ask `save-session.sh` (without `--force`) to flush if there is anything new and the cooldown has elapsed. `save-session.sh`'s own cooldown and lock (`mkdir`, timeout 0 on a plain call) make a repeated call a safe, silent no-op when there is nothing to do -- confirmed live: a single print-mode turn (one human message) correctly logged `1 human msgs < 3, skip` rather than saving prematurely.

**No genuine session-end signal was found.** Of the four events confirmed loading and firing (`SessionStart`, `PreInvocation`, `PostInvocation`, `Stop`), none is a process-exit or conversation-close event -- `Stop` fires after every turn including the first. This is reported as an open gap, not silently worked around: a long Antigravity conversation that never has three human turns before the process exits (or exceeds the cooldown one final time) has no equivalent of Claude Code's last-chance `SessionEnd --force` flush. Whether Antigravity has such a signal at all -- a process-exit hook, a `conversationId`-scoped finalize event -- was not found in `agy changelog` or in Google's own published hooks docs at 1.1.27, and neither source is exhaustive.

## Three live defects found and fixed while driving this against a real `agy` process

None of these three is visible from a `pipeline/host.py`/`pipeline/extract.py` unit test alone -- they are properties of the hook-process composition, and were only found by actually running `agy` against the installed manifest and reading `~/.gemini/antigravity-cli/cli.log` and the target workspace's own `.remember/logs/`, never by trusting a hook's exit code.

1. **Antigravity's hook executor parses a command hook's STDOUT as protojson against its own schema, not Claude Code's.** Delegating straight to `session-start-hook.sh` (plain-text `=== REMEMBER ===` context injection) and `user-prompt-hook.sh` (a `{"hookSpecificOutput": ...}` envelope) made `agy` log `failed to unmarshal result from hook jsonhook__remember_SessionStart_0_0 via protojson: proto: syntax error (line 1:1): invalid value =` and the equivalent `unknown field "hookSpecificOutput"` for `PreInvocation`, on every single invocation. Fixed by discarding the delegate's stdout in both adapters (`>/dev/null`) -- context injection through hook stdout is a Claude Code-specific mechanism this port does not attempt (#563's own scope is capture, not context injection), and no Antigravity-native equivalent contract is documented to target instead.
2. **A plain backgrounded subshell did not survive the hook process exiting.** `agy-stop-hook.sh` originally launched `save-session.sh` with `( bash ... & )`; across repeated live probes, no trace of it ever starting appeared in the target workspace's own memory log, even though the parent hook exited 0. Fixed with `nohup ... &` + `disown`, the exact defence `post-tool-hook.sh` and `session-end-hook.sh` already use for their own backgrounded saves -- Antigravity's own hook executor appears to reap a command hook's process group more aggressively than Claude Code's did.
3. **`save-session.sh` has no stdin of its own to resolve `PROJECT_DIR` from when launched this way**, and FATALs loudly (`Cannot resolve project root`) if `CLAUDE_PROJECT_DIR`/`REMEMBER_HOOK_CWD` are both unset -- a FATAL the `nohup ... >/dev/null 2>&1` redirect above was silently swallowing until caught by running the hook by hand with output not redirected. Fixed by forwarding Antigravity's own `workspacePaths[0]` as `CLAUDE_PROJECT_DIR` in `agy-stop-hook.sh`, the same field `agy-session-start-hook.sh` already forwards as `cwd`.

## `workspacePaths` is populated only by `--add-dir`, not by process `cwd`

Running `agy -p "..."` from inside a directory does **not** put that directory in the `SessionStart`/`Stop` payload's `workspacePaths` array -- it stays `[]`, confirmed by comparing a bare `cd <dir> && agy -p ...` against the same command with `--add-dir <dir>` appended. Without a workspace path, `agy-session-start-hook.sh` and `agy-stop-hook.sh` have no `cwd`/`CLAUDE_PROJECT_DIR` to forward, and `resolve-paths.sh` FATALs. A real Antigravity session run interactively (rather than via `-p`) may add the working directory automatically -- not established here, print mode only.

## End-to-end capture, observed live

A real `agy -p "reply with exactly: X" --add-dir <workspace>` turn correctly fired `SessionStart`, `PreInvocation`, and `Stop` (in that order, per `~/.gemini/antigravity-cli/cli.log`), bootstrapped `<workspace>/.remember/`, and `save-session.sh`'s own extraction step read the real `transcriptPath` Antigravity handed the `Stop` hook and reported `2 exchanges (1 human)` -- matching the turn exactly (`[HUMAN] reply with exactly: X`, `[AGENT] X`, confirmed via `--dry`) -- then correctly skipped the actual save because one human turn is below the default `min_human_messages: 3` threshold. That skip is the CORRECT behaviour for `Stop` firing after a single turn, not a defect: it is the same threshold every other host's incremental save already respects, and it is what makes calling `save-session.sh` (without `--force`) safe to do on every `Stop`.

## Summarization: an Antigravity session is summarized by `claude`, not by `agy` or Gemini

There is no Antigravity-native summarizer in this plugin -- `REMEMBER_SUMMARIZER` only ever resolves to `claude` or `codex` (see [`docs/configuration.md`](configuration.md)). Under the default `auto`, an Antigravity transcript is a recognised, non-default-host case -- the same shape #460/#477 already warn about for Codex -- so it now logs a warning naming the transcript before falling back to `claude -p` ([#567](https://github.com/Digital-Process-Tools/claude-remember/issues/567)), the same as a vanished-Codex-transcript does. This means an authenticated `claude` CLI is required for summarization even on a machine that otherwise only runs `agy`, and the session is billed to Anthropic, not to whichever provider `agy` itself is configured against. Set `REMEMBER_SUMMARIZER=claude` explicitly to silence the warning if this is the intended, permanent configuration.

## Not established

- **Whether Antigravity has any genuine session-end signal at all** -- see above.
- **Whether `<workspace>/.agents/hooks.json` (workspace-local, needing the folder trusted) loads through the same code path** as the shared file -- untested, per `.claude/jit-context/vocabulary/02-hosts/antigravity-hooks.md`.
- **`PreToolUse`/`PostToolUse`** -- confirmed unstable/unavailable on 1.1.27 (do not parse under the current schema; fired once, denying every tool call, on `agy` 1.0.5 per manaflow-ai/cmux#5358). Not built on; if this plugin's `PostToolUse` capture has a real Antigravity analogue, it is not this one.
- **Everything above is macOS darwin/arm64, `agy` 1.1.27, this session, 2026-09-05.** Nothing here is claimed for Linux, Windows, or any other `agy` version -- the schema alone changed twice between 1.1.26 and 1.1.27 (#553), and could change again.
