---
title: "A hook notice must never claim a tree state it did not actually check"
description: "#946: a git-status call hidden behind 2>/dev/null, piped into a while loop, reads as a clean tree on either a genuinely clean tree or a failed git call -- yet the hook's own notice says 'the tree is unchanged' either way. The same wrong-attribution shape fired for real at two more sites in the same round (#945's restore-scoped-to-slug and symbolic-ref findings): a notice claiming only $SLUG was reset, or that HEAD was restored correctly, when neither was actually verified."
match: hooks\.d/.*\.sh$
---

Three confirmed instances in `hooks.d/after_save/60-git-reconcile.sh` of the same pattern: a
notice/log line asserts a specific tree state ("the tree is unchanged", "only $SLUG was reset",
"the rebase state was cleared") that was inferred from the *absence* of an error, or from one
more unverified git call, rather than from an actual check of what changed.

- `_grc_foreign_changes` pipes `git status --porcelain` through `2>/dev/null` into a `while`
  loop -- an empty result from a failed call looks identical to a clean tree.
- The scoped restore (`git checkout`/`rm` limited to `$SLUG/`) does not confirm no *other*
  path was touched by the rebase before claiming "only $SLUG was reset" -- reproduced: another
  slug silently reverted or left with live conflict markers while the notice said only `$SLUG`
  changed.
- `git symbolic-ref HEAD refs/heads/$BRANCH_NAME` is not checked against what the rebase itself
  recorded (`rebase-merge/head-name`) before the notice claims HEAD was correctly restored.

**Before writing a notice or log line in a script here that claims a specific tree/branch state
("unchanged", "reset", "restored", "cleared"), confirm the claim is backed by a git call whose
exit status and output were actually checked for this exact case** -- not merely that no error
surfaced, and not inferred from a different call that happened to succeed. If the check that
would back the claim does not exist yet, write the weaker, actually-true statement instead
("a rebase was attempted" rather than "the tree is unchanged").
