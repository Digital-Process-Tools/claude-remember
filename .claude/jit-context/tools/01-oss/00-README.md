---
title: "There is deliberately no rule keyed on the Agent tool -- yet"
description: "A tools rule on Agent can fire as of claude-jit-context 0.5.0, matched against subagent_type. What one should say is undecided, so this layer ships none."
---

**Nothing in this layer is keyed on `Agent`. That is a decision, and the reason for it has
changed** -- so if you have read an older copy of this file, read this one instead.

A rule that fired on agent dispatch would be worth having: it would put the standing clauses
of a brief in front of the dispatcher at the one moment they change behaviour, instead of
being re-typed from memory.

## What the hook does, measured

The PreToolUse hook builds the subject its tool rules match against from five `tool_input`
keys, taken in this fallback order:

| key | carried by |
| --- | --- |
| `command` | `Bash` |
| `skill` | `Skill` |
| `file_path` | `Read`, `Edit`, `Write` |
| `pattern` | `Glob`, `Grep` |
| `subagent_type` | `Agent` |

`subagent_type` is the fifth and it is **the only one of an `Agent` payload's three fields
that is read**. `description` and `prompt` are a deliberate no upstream: they are
author-written prose, so a `forbid`/`require` rule written about commands would trip on a
prompt that merely mentions one, and a prompt is large enough to cost real time in the
matcher. So a `tool: Agent` rule matches against the dispatched agent's name and nothing
else -- it *can* key on one kind of dispatch, and it can see nothing about what was asked.

**This was not always true.** Before `claude-jit-context` 0.5.0 the subject was built from
the first four keys only, an `Agent` payload produced an empty one, and the hook exited
before the layer loop: a `tool: Agent` row indexed cleanly, listed healthy in every
diagnostic, and never once fired. If the version installed where you are reading this
predates that, everything below is still blocked. A subjectless dispatch is no longer
silent either -- the hook now names the rules it could not reach, rather than answering the
`{}` that a genuine no-match also answers.

## Re-measure rather than trusting this file

Point `CLAUDE_PROJECT_DIR` at a tree holding a layer with an `Agent` rule and a `Bash` rule,
and drive the hook twice:

```
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | bash .../pre-tool-hook.sh
printf '%s' '{"tool_name":"Agent","tool_input":{"subagent_type":"x","prompt":"y"}}' | bash .../pre-tool-hook.sh
```

The `Bash` call is the control. If it says nothing either, the harness is blind and the
second answer means nothing. Give the `Agent` rule a `match:` that covers `x` and a second
`Agent` rule whose `match:` does not, or a single answer tells you a rule fired without
telling you what it fired *on*.

## Why no rule is shipped here anyway

Two questions, neither answered by the measurement above, and both wanting their own review
rather than a rider on the change that took this record off its false claim:

- **What it would say.** The standing clauses live in the agent definition being dispatched.
  A rule that restated them would be the second copy -- the one that drifts and the one
  people quote -- and one that only points at them has to name a location, which for an
  agent definition is a path inside an installed plugin rather than anything in this
  repository.
- **What it would cost.** It fires on every matching dispatch, and the benefit is asserted
  rather than observed. That is the wrong way round for something injected into every
  delegation.

**If either question gets answered, this file is what a rule replaces.** It is not edited
here: this whole layer is generated and replaced wholesale on every install, so a correction
made in this directory is gone the next time the owning plugin writes it. Report it instead.
