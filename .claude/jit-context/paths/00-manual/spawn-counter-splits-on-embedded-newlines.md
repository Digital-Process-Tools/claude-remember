---
title: "A multi-line quoted jq/awk/sed filter argument inflates every spawn count that logs \"$*\" and splits on newlines"
description: "One jq process writing a filter argument with 4 embedded newlines logged as 5 lines in the spawn-count harness, phantom-failing the macOS-only spawn-budget test while every other CI leg's own unrelated savings coincidentally masked the same miscount."
match: (scripts/.*\.sh|tests/.*\.py)$
---

`tests/spawn_counting.py`'s shim logs every spawn as `printf "%s %s\n" "$name" "$*"`, and `"$*"`
faithfully includes any literal newline characters embedded in an argv value (bash single-quotes
preserve them literally). `spawns()` counts non-blank lines in that log. Write a jq/awk/sed filter
as a multi-line quoted shell string --

```
jq -s --argjson x "$y" '
    (if $x then ... else . end)
    | reduce .[] as $z ({}; . * $z)
    | with_entries(...)
' "${files[@]}" > "$out"
```

-- and that ONE process, with FOUR embedded newlines in its filter argument, writes FIVE physical
lines to the spawn log: a phantom +4 that has nothing to do with how many processes actually ran
(#732). This only failed CI on the macOS legs specifically, because macOS's origin/main baseline
had the least slack against its spawn budget (3, vs. more on other platforms from an unrelated
saving); the same phantom miscount was silently present everywhere the multi-line string shipped,
just not loud enough elsewhere to trip a budget. It also did not reproduce on a maintainer's local
run, because the local `bash` on PATH resolved to a newer Homebrew build rather than the CI
runner's stock macOS `/bin/bash` -- reproducing it required prefixing PATH with `/bin` explicitly.

**The fix has no functional cost:** jq (and awk, and sed) is whitespace-insensitive, so collapsing
a multi-line filter argument to one line changes nothing about what it does. Before writing a
jq/awk/sed filter as a shell heredoc-like multi-line quoted string anywhere in this repo, check
whether any test that counts spawns by splitting a recorded `"$*"`-style log on newlines could see
it (`tests/spawn_counting.py`'s `spawns()` is the one shared implementation, used by at least a
dozen test files) -- and if so, write the filter on one line instead.
