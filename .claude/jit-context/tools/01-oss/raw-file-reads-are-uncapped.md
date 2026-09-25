---
title: "A raw sed/cat/head/tail/grep read has no window cap and no receipt"
description: "One lane read 462 KB through sed -n, cat and grep, every byte held to turn 1,132. supertool read/grep are capped, ranged and logged; the raw forms are refused at command position."
tool: Bash
match: ~(^|[;&])[[:space:]]*(sed[[:space:]]+-n|(cat|head|tail)[[:space:]]+[^<>|;&\n]*([;&\n]|$)|grep[[:space:]]+[^<>|;&\n]*([;&|\n]|$))
mode: block
requires: supertool
---

**Refused, not a dead end: resend the same question as the op below.**

Every byte a read returns stays in this lane's context for every later turn.
Measured on one lane (#1499): `sed -n` 105 calls / 277 KB, `cat` 26 / 130 KB,
`grep` 126 / 55 KB -- 462 KB of uncapped raw reads, carried through 1,132 turns.
`supertool read` caps at 20,000 bytes, takes a line range, and logs the call.

| you typed | send instead |
| --- | --- |
| `sed -n 10,40p FILE`, `head -30 FILE`, `tail -20 FILE` | `read:FILE:10:40` |
| `cat FILE` | `read:FILE` (whole file, capped -- add a range when you know it) |
| `grep PAT FILE` | `read:FILE:::grep=PAT` (lines in one file) |
| `grep -rn PAT DIR` | `grep:PAT:DIR:N:M` (N lines before, M after) |
| `cat FILE1 FILE2` | `batch:@FILE` with one `read` per path |

Matched only at command position -- the start of the call, or after `;` / `&&`: `x | grep PAT`
and `cmd | sed -n` are filters over a result you already paid for and are not refused, and a
line inside a heredoc body that happens to start with `cat` is content, not a command. `cat > FILE <<EOF` carries `>` and is a
write -- `python-heredoc-writes-are-unvalidated.md` owns that one.

**Not covered, on purpose:** `python3 -c` / `python3 -` that opens and prints a file, `awk`,
`less`. A miss is the safe direction here; a wrong block teaches routing around it (#1221).
