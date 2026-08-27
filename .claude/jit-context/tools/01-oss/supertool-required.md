---
title: "Read, Edit, Write, Glob and Grep go through supertool"
description: "supertool has an op for every one of these; the call is refused and the reader is told which op replaces it -- and, if none of them run, how to tell a missing binary from a missing entry point."
tool: Read|Edit|Write|Glob|Grep
match: ~.*
mode: block
requires: supertool
---

There is no read, edit, write, glob or grep that cannot go through `supertool`. Use the op that
replaces the call just refused:

- **Read** -- `supertool 'read:PATH'`
- **Edit** -- `supertool 'edit:@-'` (a TOML payload on stdin) or `supertool 'edit:::OLD:::NEW:::PATH'`
- **Write** -- `supertool 'paste:@-'` (a TOML payload on stdin, fields `path` and `content`) or
  `supertool 'paste:::PATH:::CONTENT'` -- `paste` creates missing parent directories and rewrites an
  existing file, so it covers both halves of a Write
- **Glob** -- `supertool 'glob:PATTERN'`
- **Grep** -- `supertool 'grep:PATTERN:PATH'`

No exception for an image, a PDF or a notebook cell: none exists in this repository today. If one
appears, that is when it gets one -- not before.

## If none of those run

**This rule cannot tell whether `supertool` is reachable from where you are, and it fires the
same way either way.** A rule is a text file the hook matches a subject against; it runs no
command. Nor did whatever wrote this file check on your behalf -- it ran once, elsewhere, on
somebody else's machine, possibly months ago. So the check is yours to make, and it is two
commands rather than one, because there are two routes and they fail differently:

```bash
./supertool 'ops'
supertool 'ops'
```

- **The first prints a list of ops.** Everything is wired; the refusal above was about your call.
- **The first is missing and the second works.** The binary is installed and *this clone* has no
  entry point. `./supertool` is gitignored on purpose -- committing it would bake one machine's
  absolute path into every other clone -- and it is created by supertool's own session-start
  hook, so a fresh clone has none until a session has been started in it. Start one, or call
  whichever spelling answers. **Nothing is missing from your installation.**
- **Neither runs.** `supertool` is **not installed** on this machine, and no op above will work
  until it is. That is a missing dependency, not a mistake in what you were doing. It is a Claude
  Code plugin and a declared dependency of the plugin that wrote this layer, so installing that
  plugin resolves it from the same marketplace.

**The presence of this file says nothing about your machine.** It is committed to this repository
and travels to every clone, including yours. It is enforced only where `claude-jit-context` is
also installed -- that plugin is what reads this layer and issues the refusal -- so a clone with
neither plugin is unaffected by it.

## The `requires: supertool` line above, and what it does today

**It is honoured.** `claude-jit-context` 0.6.0 shipped the reader `claude-jit-context#203` asked
for: `jit_missing_requires()` in `common.sh` probes every `requires:` value on `PATH` once per
hook invocation, and `pre-tool-hook.sh` folds the result into `requires_missing` per row --
`can_refuse = would_refuse && !requires_missing`, so a `mode: block` row naming a binary that did
not resolve cannot enforce its own block. It falls through to the advisory branch instead, and the
degrade is said out loud rather than happening silently: the injected `degrade_note` reads *"[jit]
This rule would normally refuse this call, but `<bin>` was not found on PATH, so it has degraded to
advisory instead of blocking. Install `<bin>` to restore enforcement."*

So on a reader's machine running `claude-jit-context` 0.6.0 or later with no `supertool` on
`PATH`, this rule no longer blocks -- it warns, by name, with the remedy in the message. On a
machine with `supertool` on `PATH`, or running an older `claude-jit-context`, nothing changes:
this rule is exactly as effective as it was before the field existed. **Measured against the
installed cache (`~/.claude/plugins/cache/dpt-plugins/claude-jit-context/0.6.0/scripts/`), not
asserted.** Re-measure before trusting this paragraph -- `#524` and `#570` (this repository) and
`claude-jit-context#203` are the history, and the field's meaning can move again the same way it
just did.

## Why `match: ~.*` and `mode: block` are not narrowed here

The same issue asked, separately, whether blocking all five tools outright is too large a
hammer even where `supertool` **is** installed -- proposing `mode: warn` for some of them,
`block` reserved for calls that would genuinely bypass a validator. That was weighed and
declined for this rule, not overlooked: the absent-binary failure mode above is the one that
turns a guard into an outage, and `requires:` answers exactly that, without touching what this
rule says to a reader who has the dependency. Weakening `block` to `warn` for a reader who
already has `supertool` would trade a working guard for a softer one to hedge against a case
`requires:` already covers once it ships -- the shape this project itself argues against
elsewhere in this repository: a `remind` on an absolute rule teaches the reader to dismiss it.

**That argument's own precondition has now been met, and the conclusion does not move.** #524
declined narrowing on the grounds that the absent-binary case would be answered "once
`requires:` ships" -- it has (#570), and the degrade described above is what answers it: a
reader without `supertool` is no longer blocked, without this rule's `block` weakening for the
reader who has it. Revisiting `block` again now would be re-litigating a question `requires:`
was written to close.
