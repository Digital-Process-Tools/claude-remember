---
title: "Pin LC_ALL=C before any byte-range or case-fold bash comparison"
description: "Two of this codebase's own helpers do case-insensitive matching or byte-range checks without pinning the locale, unlike the convention set elsewhere in the same files; and a case/glob byte-range check silently failed to match at all under an unidentified locale-adjacent condition on macOS CI."
match: scripts/.*\.sh$
---

**The established convention, and where it's missing.** `_resolve_remember_dir` and
`_set_store_root` pin `local LC_ALL=C` before doing byte-wise/bracket-range work, for the stated
reason that `nocasematch` and glob-range matching follow the active `LC_CTYPE` locale's folding
rules, not a fixed ASCII table. Two helpers doing exactly that kind of matching do not follow it:
`lib-case-divergence.sh`'s `_remember_case_fold_eq` (pre-existing, #298) and
`session-start-hook.sh`'s `_remember_th_ci_eq` (#721, duplicated from the former rather than
sourcing it, specifically to avoid pulling in the whole library for one comparison -- so the
duplicate inherited the gap rather than fixing it). Under an unusual locale (`tr_TR.UTF-8` is the
textbook case: ASCII `I`/`i` fold differently there than under `C`/`en_US`), the fold could
disagree with the filesystem's own case-insensitive semantics, which are locale-independent. Pin
`LC_ALL=C` in both, or state explicitly in the comment why one of them is exempt -- don't add a
third case-insensitive helper without pinning it either.

**The pin only protects a call made while the pinning frame is live -- bash scoping is dynamic,
not lexical.** A scanner or reviewer that credits an *enclosing* function's `local LC_ALL=C` to a
function merely *defined* inside it is wrong: the outer `local` is in effect only for calls made
while the outer frame is on the call stack, and a nested function definition outlives the call
that created it. A nested function invoked from *outside* its definer runs with the caller's
locale, not the definer's, no matter what pinning the definer itself does. (#695's own
`tests/test_locale_ranges_695.py::_is_protected` made exactly this mistake in its outward-walk
logic -- harmless today only because the one nested-function pair in this tree, `capture_was_seen`
inside `_remember_deferred_phase`, is called only from within its definer, which itself carries no
`LC_ALL=C` pin anyway.)

**A `case`/glob byte-range check can fail to match at all, silently, for reasons nobody has
identified.** `*[$'\001'-$'\037']*|*$'\177'*` as a `case` arm — a cheap in-shell control-byte
pre-check — failed to trip on GitHub's `macos-latest` runner (`macos-26-arm64`) even for a message
genuinely carrying a control byte, across every Python version in that job's matrix (#630). It
reproduced correctly under both Apple's system bash and a fresh Homebrew bash, under several
locales, on developer machines -- only the CI runner's own image showed the miss, and the
mechanism was never identified before the pattern was reverted to an unconditional `printf | tr`
flatten in the same PR. Treat this pattern shape (`case "$var" in *[$'\xxx'-$'\yyy']*)` as a cheap
byte-range pre-check) as unproven on CI's own macOS image: a `case` that fails to match runs no
branch and raises no error, so a miss here is invisible until a test specifically asserts the
branch fired.
