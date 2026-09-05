---
title: "Declined traps — paths layer"
description: "Traps curated and deliberately not promoted here, with the reason. A trap named below has been decided, not overlooked."
---

The rule builder skips this file by name, so an absence recorded here reads as a decision rather
than an oversight.

- **`524.readme-call-site-count-stale`** — declined as a rule 2026-09-05, **filed as
  [#580](https://github.com/Digital-Process-Tools/claude-remember/issues/580) instead**.
  `docs/windows.md:16` claims 10 `_remember_forward_slash` call sites; the live `grep` count was 12
  when the trap was written and is 15 today. That is a wrong number in a document, which a fix
  corrects — not a situation a rule can warn anyone out of. Injecting "the count in this file may be
  stale" on every touch of `docs/` would be noise standing in for a one-line repair.
