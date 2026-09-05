---
title: "Antigravity CLI (agy) hooks are not Claude Code hooks"
description: "agy reads a name-keyed hooks.json with a different schema, fails a Claude-shaped file silently, and Gemini CLI is no longer reachable at all on a free-tier individual account."
keywords: antigravity, agy, gemini cli, hooks json, fourth host, jsonhook
---

Measured on macOS darwin/arm64, `agy` 1.1.27, 2026-09-05. Nothing here is claimed for other
platforms or other versions -- this area changes release to release.

## The schema is name-keyed, not event-keyed

```json
{
  "probe_all": {
    "enabled": true,
    "type": "command",
    "events": ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "Stop", "PostInvocation", "SessionEnd"],
    "matcher": "*",
    "command": "bash /abs/path/probe.sh fired",
    "timeout": 20
  }
}
```

Top level is `name -> spec`. The spec's fields are `events` (a list), `matcher`, `command`, `type`,
`timeout`, `enabled` -- recoverable from the binary with
`strings -a ~/.local/bin/agy | grep -E 'json:"(events?|matcher|command|timeout|enabled)"'`.

Claude Code's shape -- an event key holding an array of matcher objects -- **does not parse**:

```
hooks_manager.go:33] failed to parse hooks.json at ~/.gemini/config/hooks.json:
json: cannot unmarshal array into Go struct field .SessionStart of type jsonhook.JSONHookSpec
hooks_manager.go:53] loaded 0 named hooks from 1 hooks.json file(s)
```

**That error reaches stdout nowhere.** `agy -p "/hooks" --output-format json` answers
`{"hooks":[]}` -- identical to having no file. The only witness is
`~/.gemini/antigravity-cli/cli.log` (a symlink into `log/`). Check it after every hooks.json edit:

```bash
grep -ih hook ~/.gemini/antigravity-cli/cli.log | tail -6
```

`loaded N named hooks` is the line that says whether the file was understood.

## Registered is not fired

`/hooks` lists the hook `enabled: true`, the log says `loaded N named hooks`, and **the hook does
not run**. Observed on 1.1.27, with tools proven to have executed in the same turn.

| turn | tool ran? | hook loaded? | hook fired? |
| --- | --- | --- | --- |
| no-tool print turn, 7 events declared | no | `loaded 1 named hooks` | no |
| `--dangerously-skip-permissions`, "run: echo hi" | **unproven** -- no tool line in the log | yes | no |
| `--dangerously-skip-permissions`, "create /tmp/agy-proof.txt", `matcher: "*"` | yes -- file created | yes | no |
| same, **matcher field removed**, events `PostToolUse` and `Stop` | yes -- two tools | `loaded 2 named hooks` | **no** |

The last row settles it. The log names the tools it auto-approved:

```
tool_confirmation_manager.go:170] Always-proceed: auto-approving tool confirmation "WriteToFile" at step 2
tool_confirmation_manager.go:170] Always-proceed: auto-approving tool confirmation "ViewFile" at step 4
```

Two tools ran, both hooks were loaded, no matcher could discard anything, and the hook's log file
stayed 0 bytes. `PostToolUse` and `Stop` do not fire from `~/.gemini/config/hooks.json`.

**Prove the tool call by its side effect, never by the reply.** The "run: echo hi" row above looked
like a passing test and was worthless -- the model answered `DONE` and the log carries no tool line.
`create the file /tmp/agy-proof.txt` leaves evidence that does not depend on what the assistant said.

Not ruled out: the binary carries a `CustomizationConfig.GetEnableJsonHooks` flag, so execution may
be gated behind an experiment -- inference from a symbol name, not a measurement. Also untested:
the workspace-local `<workspace>/.agents/hooks.json` path, which may load through different code.

`--dangerously-skip-permissions` is honoured in print mode -- the log says
`Print mode: --dangerously-skip-permissions set, auto-approving all tool permissions` -- but the
Claude Code auto-mode classifier refuses that flag, including refusing to write it into a script.
It is a human-run test, and a long pasted command wraps in the terminal and silently eats its own
`-p` argument; keep the line short.

## Paths

| what | where |
| --- | --- |
| shared hook manifest | `~/.gemini/config/hooks.json` |
| workspace-local | `<workspace>/.agents/hooks.json`, loads only after the folder is trusted |
| trusted folders | `~/.gemini/trustedFolders.json` |
| plugin install target | `~/.gemini/config/plugins/<name>/` |
| log | `~/.gemini/antigravity-cli/cli.log` |

Antigravity has no directory of its own: it shares Gemini CLI's `~/.gemini/`. Anything this repo
ships into `.gemini/` and anything `agy` writes are neighbours, and neither is evidence about the
other. A plugin's own `hooks.json` is copied in, counted (`hooks : 1 processed`) and never loaded --
only the two paths above load.

## Gemini CLI is not a reachable host

`@google/gemini-cli` 0.58.0 answers, with credentials that do work:

```
IneligibleTierError: UNSUPPORTED_CLIENT -- This client is no longer supported for
Gemini Code Assist for individuals. tierId: free-tier
```

Thrown after the token is accepted, so an auth fix is not the answer. Google's own error text names
Antigravity as the migration. Gemini manifests in this repo stay shape-tested only.

## Two free reads

`agy -p "/hooks" --output-format json` and `agy changelog` both answer in print mode without
starting a turn or spending quota. Use them before designing anything against this host.
