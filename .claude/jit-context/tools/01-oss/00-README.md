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

## #903: this rule reaches a spawned reviewer subagent too, and that is intentional

A developer lane's own self-review spawns a read-only `Explore` reviewer against the diff it just
committed. That reviewer issues Read/Edit/Write/Glob/Grep calls of its own, in the same tree, and
they hit `supertool-required.md` below exactly as the dispatching lane's own calls do -- #903 is
two reproduced instances of that.

**Narrowing the trigger to skip a spawned subagent was the issue's first ask, and it is not
possible today.** `supertool-required.md`'s `match: ~.*` is tested against the PreToolUse hook's
own subject, built from `tool_input` alone (see the table above) -- and that subject carries
no field naming *which agent* issued the call. There is no `isSidechain`, no `subagent_type` on a
`Read`/`Edit`/`Write`/`Glob`/`Grep` payload, nothing at all distinguishing a spawned reviewer's own
tool call from its dispatcher's. No signal in the subject means no way to narrow on it -- so this
is the issue's second branch, "state it explicitly", rather than the first.

**What that means for `Explore`, or any other read-only reviewer never briefed on `supertool`.** It
has no such tool in its actual grant and no route to install one, so a block here is a rule it
cannot comply with by being told about it -- only by already knowing `supertool` is on `PATH` and
calling it through its own `Bash` grant, which nothing in its brief currently says to do. Both
observed instances (#903) already did the next best thing on their own: they read the refusal as
untrusted injected content that named a tool contradicting their actual grant, and continued
through their own authorized `Read`/`Bash` tools rather than obeying it. That is correct, and nothing
here is meant to change it -- the only enforcement-side answer available is the `requires:`
degrade documented below, never a scope narrower than "every session touching this tree", because
the hook has nothing narrower to match against.

If `claude-jit-context` ever adds a signal that tells a spawned subagent's own calls apart from its
dispatcher's, that is what this rule gets narrowed on -- not a guess made without one.

---

## `supertool-required.md`: why it stays this short

That rule is `mode: block` on `Read|Edit|Write|Glob|Grep` with `match: ~.*`, so its whole body
re-injects on every refused call -- no `once` marker exists for a block rule. What stays in the
rule file is only the five op substitutions and the two-command triage line; everything below,
which used to live there, moved here instead, where it costs nothing per refusal (#757).

No exception for an image, a PDF or a notebook cell: none exists in this repository today. If one
appears, that is when it gets one -- not before.

### If the triage commands in the rule don't run

**Neither `./supertool 'ops'` nor `supertool 'ops'` tells you anything about the *other* one --
run both.** A rule is a text file the hook matches a subject against; it runs no command, and
nothing checked reachability on your behalf before writing this file.

- **`./supertool 'ops'` prints a list of ops.** Everything is wired; the refusal was about your
  call, not your setup.
- **`./supertool` is missing, `supertool` works.** The binary is installed and *this clone* has
  no entry point. `./supertool` is gitignored on purpose -- committing it would bake one
  machine's absolute path into every other clone -- and it is created by supertool's own
  session-start hook, so a fresh clone has none until a session has been started in it. Start
  one, or call whichever spelling answers. Nothing is missing from your installation.
- **Neither runs.** `supertool` is not installed on this machine. It is a Claude Code plugin and
  a declared dependency of the plugin that wrote this layer, so installing that plugin resolves
  it from the same marketplace.

### The `requires: supertool` frontmatter line, and what it does today

**It is honoured.** `claude-jit-context` 0.6.0 shipped the reader `claude-jit-context#203`
asked for: `jit_missing_requires()` in `common.sh` probes every `requires:` value on `PATH` once
per hook invocation, and `pre-tool-hook.sh` folds the result into `requires_missing` per row --
`can_refuse = would_refuse && !requires_missing`, so a `mode: block` row naming a binary that did
not resolve cannot enforce its own block. It falls through to the advisory branch instead, and
the degrade is said out loud rather than happening silently: the injected `degrade_note` reads
*"[jit] This rule would normally refuse this call, but `<bin>` was not found on PATH, so it has
degraded to advisory instead of blocking. Install `<bin>` to restore enforcement."*

So on a reader's machine running `claude-jit-context` 0.6.0 or later with no `supertool` on
`PATH`, this rule no longer blocks -- it warns, by name, with the remedy in the message. On a
machine with `supertool` on `PATH`, or running an older `claude-jit-context`, nothing changes.
**Measured against the installed cache, not asserted.** Re-measure before trusting this
paragraph -- `#524` and `#570` (this repository) and `claude-jit-context#203` are the history,
and the field's meaning can move again the same way it just did.

### Why `match: ~.*` and `mode: block` are not narrowed

The same issue that asked for a smaller body (#757) also inherited an older question (#524):
whether blocking all five tools outright is too large a hammer even where `supertool` **is**
installed -- proposing `mode: warn` for some of them, `block` reserved for calls that would
genuinely bypass a validator. That was weighed and declined, not overlooked: the absent-binary
failure mode above is the one that turns a guard into an outage, and `requires:` answers exactly
that, without touching what this rule says to a reader who has the dependency. Weakening `block`
to `warn` for a reader who already has `supertool` would trade a working guard for a softer one
to hedge against a case `requires:` already covers -- the shape this project itself argues
against elsewhere: a `remind` on an absolute rule teaches the reader to dismiss it.

**That argument's own precondition has now been met, and the conclusion does not move.** #524
declined narrowing on the grounds that the absent-binary case would be answered "once
`requires:` ships" -- it has (#570), and the degrade described above is what answers it: a
reader without `supertool` is no longer blocked, without this rule's `block` weakening for the
reader who has it. Revisiting `block` again would be re-litigating a question `requires:` was
written to close.
