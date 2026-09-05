---
title: "Declined traps — vocabulary layer"
description: "Traps curated and deliberately not promoted here, with the reason. A trap named below has been decided, not overlooked."
---

The rule builder skips this file by name, so an absence recorded here reads as a decision rather
than an oversight. If you hit one of these again, the answer is already taken — reopen the decision
rather than re-filing the trap.

- **`456.gemini-cli-headless-auth-invalid-grant`** — declined 2026-09-05. `gemini-cli` 0.57.0's
  cached OAuth credentials failed with `invalid_grant` and no headless escape hatch, costing ~20
  minutes. Declined because the premise is dead, not because the observation was wrong: #532 is
  closed, 0.58.0 refuses free-tier individual accounts outright with `IneligibleTierError:
  UNSUPPORTED_CLIENT` before auth is even reached, and Gemini CLI was removed from the README
  entirely in #572. A rule warning about an auth wall on a host this repo no longer documents would
  fire for nobody. The auth detail survives in `docs/install-gemini-cli.md` and #532.
