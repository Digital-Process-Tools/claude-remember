---
title: "Antigravity CLI (agy) hooks: schema, firing, and the port that uses it"
description: "agy 1.1.27 accepts a name-keyed hooks.json with event-keyed handler arrays, fires four events (SessionStart/PreInvocation/PostInvocation/Stop) confirmed live, and Gemini CLI is no longer reachable at all on a free-tier individual account. #563 ports Remember's capture onto this; see docs/install-antigravity.md."
keywords: antigravity, agy, gemini cli, hooks json, fourth host, jsonhook
---

Measured on macOS darwin/arm64, `agy` 1.1.27, 2026-09-05. Nothing here is claimed for other
platforms or other versions -- this area changes release to release, and already changed twice
between 1.1.26 and 1.1.27 in this repo's own investigation (#553).

## The schema is name-keyed AND event-keyed, one layer removed from Claude Code's

**This corrects an earlier version of this entry**, which showed a `name -> {events: [...]}`
shape (a single flat `events` list per name, reverse-engineered from `strings -a` on the binary)
and reported that `PostToolUse`/`Stop` load but never fire against it. That shape and that finding
are both superseded (#553's own last comment, #563): the schema `agy` 1.1.27 actually accepts, and
under which all four events below were observed firing, is a hook **name** mapping to an object
whose keys are **event names**, each holding an **array of handler objects** -- Claude Code's own
shape one level down, not a flat `events` list at all:

```json
{
  "remember": {
    "enabled": true,
    "SessionStart": [ { "type": "command", "command": "bash /abs/path/probe.sh SessionStart", "timeout": 30 } ],
    "PreInvocation": [ { "type": "command", "command": "bash /abs/path/probe.sh PreInvocation", "timeout": 30 } ]
  }
}
```

Claude Code's own shape -- an event key holding an array of matcher objects, at the TOP level
rather than nested one layer under a hook name -- still does not parse at all:

```
hooks_manager.go:33] failed to parse hooks.json at ~/.gemini/config/hooks.json:
json: cannot unmarshal array into Go struct field .SessionStart of type jsonhook.JSONHookSpec
hooks_manager.go:53] loaded 0 named hooks from 1 hooks.json file(s)
```

An unrecognised *event* inside an otherwise valid file is dropped the same silent way: the
`/hooks` listing simply prints fewer rows than the file declares, with no warning.

**That error reaches stdout nowhere.** `agy -p "/hooks" --output-format json` answers
`{"hooks":[]}` -- identical to having no file. The only witness is
`~/.gemini/antigravity-cli/cli.log` (a symlink into `log/`). Check it after every hooks.json edit:

```bash
grep -ih hook ~/.gemini/antigravity-cli/cli.log | tail -6
```

`loaded N named hooks` is the line that says whether the file was understood.

## Firing is confirmed for four events; three more are visible in the log but never delivered

`SessionStart`, `PreInvocation`, `PostInvocation` and `Stop` all fired on a single print-mode turn
under the corrected schema above -- no tool call, no `--dangerously-skip-permissions` needed for
any of the four. `PreToolUse`/`PostToolUse` do not even LOAD under this schema (silently dropped,
same as any unrecognised event); `UserPromptSubmit`/`SessionEnd`/`Notification`/`turn-completion`
load under some schema variants but were never observed to fire. **Prove firing by a side effect,
never by the reply** -- "run: echo hi, then say DONE" produced a `DONE` with an empty hook log and
no tool line anywhere, which looked like a pass and proved nothing; `create the file
/tmp/agy-proof.txt` (or append a fixed line to a fixed path) leaves evidence that does not depend
on what the assistant said. `#563`'s port (`docs/install-antigravity.md`) is the live consumer of
this: `scripts/agy-session-start-hook.sh`, `scripts/agy-pre-invocation-hook.sh` and
`scripts/agy-stop-hook.sh`.

`--dangerously-skip-permissions` is honoured in print mode -- the log says
`Print mode: --dangerously-skip-permissions set, auto-approving all tool permissions` -- but the
Claude Code auto-mode classifier refuses that flag, including refusing to write it into a script.
It is a human-run test, and a long pasted command wraps in the terminal and silently eats its own
`-p` argument; keep the line short. Not needed for any of the four events actually ported.

## A firing hook's own STDOUT is parsed as protojson against `agy`'s schema, not read as text

Delegating a `SessionStart`/`PreInvocation` handler straight to a Claude Code-shaped hook script
(plain-text context injection, or a `{"hookSpecificOutput": ...}` stdout envelope) makes `agy` log
`failed to unmarshal result from hook ... via protojson: proto: syntax error (line 1:1): invalid
value =` (or `unknown field "hookSpecificOutput"`) on **every single invocation** -- confirmed
live, #563. `agy` does not treat a command hook's stdout as opaque context the way Claude Code
does; it tries to decode it as its own structured result. A hook command that has nothing
Antigravity-shaped to say should discard its own stdout (`>/dev/null`) rather than let a Claude
Code-formatted envelope reach the parser.

## A backgrounded save must survive the hook process exiting, and `workspacePaths` needs `--add-dir`

A plain `( cmd & )` backgrounded from inside a command hook did not survive the hook process
exiting under a real `agy` run -- no trace of the backgrounded command ever starting, across
repeated live probes, even though the hook itself exited 0. `nohup cmd & disown` (the same defence
already used elsewhere in a hook script that backgrounds work) does survive. Separately,
`workspacePaths` on the `SessionStart`/`Stop` stdin payload is `[]` for a bare `cd <dir> && agy -p
...` -- it is populated only by an explicit `agy ... --add-dir <dir>`, confirmed by running both
forms back to back from the same directory.

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

## `agy plugin validate` says `[ok]` over a total absence

`agy plugin validate .claude-plugin` against a directory holding only `plugin.json` -- no
`skills/`, `agents/`, `commands/`, `mcpServers` or `hooks/` -- prints:

```
  [ok]    .claude-plugin
          - skills      : skipped (not found)
          - agents      : skipped (not found)
          - commands    : skipped (not found)
          - mcpServers  : skipped (not found)
          - hooks       : skipped (not found)
```

Green `[ok]`, exit code `0`. A directory that resolved **zero** components validates identically to
one that resolved every component correctly (`agy` 1.1.26, #554).

**The `skipped` lines are the reading; the verdict above them is not derived from them.** It is a
claim about what was *checked*, not about what was *found*. Never cite a green `agy plugin validate`
run, or its exit code, as evidence that an Antigravity install loaded anything -- read the
per-component detail lines.

Same shape as this repo's own worst case: something that ran, produced a clean result, and saved
nothing. Here it is in a third-party tool rather than ours. Not fixable from this repo; no upstream
channel was looked for.
