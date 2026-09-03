---
title: "Read, Edit, Write, Glob and Grep go through supertool"
description: "supertool has an op for each of these; the refusal names the replacement, and 00-README.md covers the rest."
tool: Read|Edit|Write|Glob|Grep
match: ~.*
mode: block
requires: supertool
---

No read, edit, write, glob or grep bypasses `supertool`. Use the op that replaces
the refused call:

- **Read** -- `supertool 'read:PATH'`
- **Edit** -- `supertool 'edit:@-'` (TOML on stdin) or `supertool 'edit:::OLD:::NEW:::PATH'`
- **Write** -- `supertool 'paste:@-'` (TOML on stdin, `path`/`content`) or
  `supertool 'paste:::PATH:::CONTENT'` -- creates missing dirs, rewrites an
  existing file, so it covers both halves of a Write
- **Glob** -- `supertool 'glob:PATTERN'`
- **Grep** -- `supertool 'grep:PATTERN:PATH'`

If none of those run, triage with `./supertool 'ops'` then `supertool 'ops'` --
see `00-README.md` here for what each answer means and why this rule stays
this wide.
