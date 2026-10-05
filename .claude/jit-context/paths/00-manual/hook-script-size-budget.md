---
title: "A hook script (or a library a hook compiles in) has a 120 KiB release budget, 128 KiB observed scanner limit"
description: "The Anthropic directory's scanner stops following a hook script past 128 KiB and holds it as COMMAND_SCRIPT_NOT_FOLLOWED. check_release_tree.py FAILs any hook script over 120 KiB (122,880 bytes) at release time and in every PR's pytest matrix. Headroom is small: shrink real code, never minify, and watch the scanner's other known triggers while you are in here."
match: (scripts|hooks|hooks\.d)/.*\.sh$
---

**Budget, not headroom to spend.** Observed 2026-10-05 across 21 portal probes: every hook script
>= 131,120 bytes held as `COMMAND_SCRIPT_NOT_FOLLOWED`, every one <= 130,955 bytes cleared -- the
scanner's real cutoff is 128 KiB (131,072 bytes). `check_release_tree.py`'s
`_check_hook_script_size` FAILs anything over `HOOK_SCRIPT_MAX_BYTES` (120 KiB, 122,880 bytes) --
a margin under the observed limit, not the limit itself. This runs at release time
(`build_release_tree.py` + `check_release_tree.py`) AND pre-merge, in the ordinary `pytest` job's
`tests/test_strip_shell_comments_900.py::test_this_repository_built_tree_has_no_check_failures`,
which builds the release tree and asserts zero offenders on every PR (#904).

**A "hook script" is bigger than the file you are editing.** It is each of the four
`HOOK_SCRIPT_NAMES` (`session-start-hook.sh`, `session-end-hook.sh`, `user-prompt-hook.sh`,
`post-tool-hook.sh`) PLUS every library its own `source`/`.` chain pulls in -- `compile_hooks.py`
inlines each sourced file as one function in the built hook. Editing a library (`log.sh`,
`lib-memory-context.sh`, `bootstrap-dirs.sh`, `lib-memory-dir.sh`, `lib-lock.sh`, ...) grows every
hook that sources it, not just the library file's own size on disk.

**Check before you add, not after:** `python3 .github/scripts/compile_hooks.py --repo .` prints
each hook's compiled size with nothing written. As of #899, `session-start-hook.sh` built to
121,820 bytes -- about 1 KiB of headroom under the 120 KiB budget.

**The fix is less real code.** Comments are already stripped by this point; squeezing whitespace
or renaming variables only hides the growth until the next feature and was refused by the agent's
own safety classifier as scanner evasion when tried. Move work out of the hook's source chain
instead.

This entry also fires on `hooks/` and `hooks.d/*.sh` -- `check_release_tree.py`'s other
scanner-shape needles (below) scan every `.sh` under `_SCRIPT_DIRS` (`hooks`, `hooks.d`,
`scripts`), not only the four compiled hooks and their libraries; the 120 KiB size budget above
is still scoped to those four plus what they `source`.

**The scanner's other known triggers, while you are editing a hook or a library it compiles in:**
- a one-line reporter whose whole body is a single call to another function with a positional
  parameter (`$1`..`$9`, `${1}`..) spliced into a string argument -- `#905`,
  `_check_single_line_delegate_positional` in `check_release_tree.py`. A bare `"$1"` is fine; `"ERROR: $1/$2"` is the held shape.
- a `case` statement (`_check_case_statement`) -- rewrite as an `if`/`elif` ladder of `[ ]` tests.
- a typed `<<` here-document (`_check_typed_heredoc`) -- the scanner cannot place where it ends;
  `<<<` here-strings are fine, `$(( x << 4 ))` arithmetic is fine.
- naming another shipped script by its path in a comment or string (`_check_hook_names_other_hook`)
  -- the other half of `COMMAND_SCRIPT_NOT_FOLLOWED`, scoped to the four hooks.json-registered
  scripts.
