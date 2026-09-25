---
title: "The manual tools layer — what lives here, and what was declined"
description: "Human-written tool rules, safe from the generated 01-oss layer's wholesale replacement. Declined traps are recorded below rather than left silent."
---

**Why this layer exists at all.** `01-oss/` is generated and replaced wholesale on every plugin
install (`oss_rules.py`: "the layer is replaced wholesale on every install, because nothing a human
wrote lives in it"). An append there is destroyed silently by the next update — no error, no diff,
the knowledge simply gone. Anything a human writes goes here instead.

Two rules here are **interim copies of knowledge that belongs upstream**, because the situations
they describe belong to a plugin rather than to this repository, and every repo that plugin manages
hits them. They are filed upstream and should be deleted here once they ship in the generated layer:

- `tree-snapshot-compare.md` → [claude-oss#1105](https://github.com/Digital-Process-Tools/claude-oss/issues/1105)
- `release-publish-denied.md` → [claude-oss#1106](https://github.com/Digital-Process-Tools/claude-oss/issues/1106)
- `supertool-literal-backslashes.md` → [claude-supertool#2327](https://github.com/Digital-Process-Tools/claude-supertool/issues/2327)
- `raw-append-write-unguarded.md` → [claude-supertool#2699](https://github.com/Digital-Process-Tools/claude-supertool/issues/2699)

## Declined traps

The rule builder skips this file by name, so an absence recorded here reads as a decision rather
than an oversight.

- **`487.ostype-immutable-on-windows-gitbash`** — declined 2026-09-05. `$OSTYPE` could not be
  forced away from its real value inside bash under real Windows Git Bash: neither
  `subprocess.run(env={"OSTYPE": ...})` nor `local OSTYPE=...` inside a function had any effect,
  across two separate CI runs (jobs 100892436094 and 100895310415, PR #499), costing two full CI
  round-trips. `readonly -p` shows `OSTYPE` is not readonly, so the mechanism was never explained.
  Well-evidenced and genuinely surprising — declined anyway because the lesson is already encoded
  where it bites: the test carries `@pytest.mark.skipif(sys.platform == "win32", ...)`, so the next
  lane meets the answer at the point of failure rather than needing a warning beforehand. A rule
  firing on every Windows-conditional bash test would restate a skip that is already there.
  **Reopen this if someone tries the same override a second time** — a repeat would mean the skip
  is not visible enough, and that is a different finding from the one declined here.

- **`722.edit-tool-cannot-write-raw-unicode-literal`** — declined 2026-09-25. A literal
  zero-width-space character written through `edit:@-`'s TOML payload landed on disk, then
  ruff's PLE2515 correctly flagged it (a legitimate lint, not a supertool bug) — costing one
  round-trip until the escape-sequence text (`\u200b`) was written instead of the literal
  character. Declined as a rule: this is not a code defect (nothing in this repo needs fixing),
  and the workaround is a single, already-obvious habit ("write the escape sequence text, not
  the invisible character") not worth injecting into every `edit:@-` call — most payloads never
  carry an invisible/ambiguous unicode character at all.
- **`725.recon-reads-dirty-sibling-clone-not-origin-main`** — declined 2026-09-25 as a rule in
  *this* repository: the defect is in `oss:recon` (agents/recon.md, Digital-Process-Tools/
  claude-oss), which has no instruction pinning its reads to `origin/<default-branch>` rather
  than whatever tree its own invoking process's cwd resolves to — still true as of claude-oss
  0.42.0, confirmed by reading `agents/recon.md` directly. Not this repository's code to fix, and
  not a lesson a rule injected into *this* repo's own sessions would ever reach (recon runs as
  its own spawn, elsewhere). **Filed as
  [claude-oss#1745](https://github.com/Digital-Process-Tools/claude-oss/issues/1745) instead,**
  naming `commands/run/curate.md`'s own worktree-first fix (#1670) for the identical class as
  the template recon could adopt.
