---
title: "A security-relevant check gated on a shelled-out command must not fold 'command failed' into its permissive branch"
description: "git ls-files guarded by 2>/dev/null and returning 1 on any non-zero exit collapses 'not tracked' and 'git could not answer at all' into the same observable outcome, reaching the permissive branch on both a clean negative and an error."
match: scripts/.*\.sh$
---

`_remember_handoff_is_tracked` (#721) decides whether a handoff file is tracked by asking git,
guarding both the exact-case check (`git ls-files --error-unmatch ... >/dev/null 2>&1`) and the
case-insensitive fallback (`git ls-files -z ... 2>/dev/null`, falling through to `return 1` on no
match) so that ANY non-zero exit -- a corrupt object, an interrupted process, a repository mid-gc,
not just a genuine "no match" -- reads as "untracked", and the caller then delivers the handoff as
trusted. The permissive branch is reached on both a clean negative and an error, which is the
wrong-direction default for a check that exists to refuse something unsafe.

**This codebase already has a considered answer for the identical hazard, one file away.**
`lib-case-divergence.sh`'s `REMEMBER_CASE_STATUS` uses three states -- `ok` / `diverged` /
`unavailable` -- never folding `unavailable` into `ok`, and its own header comment states the
rule outright: "An absence produced by this check must never read as agreement; that conflation
is this repo's recurring defect (#296, #299, and the 0.12.0 entry's 'three states, not two')."

**Before writing (or reviewing) any check that shells out to decide something security- or
trust-relevant** -- tracked/untracked, present/absent, verified/unverified -- give it three
states rather than two: the positive, the negative, and "could not tell", with the last one
routed to refuse or say so, never silently merged into the negative. A `2>/dev/null` or a bare
`|| return 1` on a git/external-command call is the shape that merges them; if you find one gating
a permissive branch, that is the thing to check first.
