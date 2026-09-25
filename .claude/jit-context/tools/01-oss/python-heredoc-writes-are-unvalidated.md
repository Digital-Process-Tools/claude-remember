---
title: "A python heredoc or cat > that writes a file skips every validator and is paid twice"
description: "One lane sent 253 KB of python3 - <<EOF write payloads: no jsonlint, no ruff, no gitleaks, no rollback, and the payload re-sent on every later turn. edit:@- and paste:@- are the same bytes with a receipt."
tool: Bash
match: ~(^|[;&|])[[:space:]]*(cat[[:space:]]*>|python3?[[:space:]]+-[[:space:]][^\n]*<<[[:print:][:space:]]*(open[(][^\n]*['"][wa]['"]|write_text[(]|[.]write[(]))
mode: block
requires: supertool
---

**Refused, not a dead end: send the same bytes through `edit:@-` or `paste:@-`.**

`.supertool.json` hooks `jsonlint`, `ruff`, `gitleaks` and `rollback_on_fail` into `edit`,
`replace`, `replace_lines`, `paste`, `append` and `vim`. A `python3 - <<EOF` that calls
`open(..., "w")` / `write_text(...)` / `.write(...)`, or a `cat > FILE <<EOF`, reaches the
file through no op: nothing validates it, nothing rolls it back, and no receipt names what
changed (#1075, #1055, #1333). Measured on one lane (#1499): 120 such heredocs, 253 KB of
payload, each re-sent on every later turn.

| you typed | send instead |
| --- | --- |
| `cat > PATH <<'EOF' ... EOF` (new file, or whole file) | `paste:@-` with `path` and `content` |
| `python3 - <<'EOF'` that rewrites one region | `edit:@-` with `path`, `old`, `new` -- one payload per edit |
| several regions in one file | `batch:@FILE`, one `{op = "edit", ...}` per region |
| a script that must run to compute the content | run it to stdout, then `paste:@-` the result |

`python3 - <<EOF` that only **reads** (analysis, a probe, printing JSON) is not matched:
the write pattern has to appear in the body. Matched at command position only -- the start
of the call, or after `;` / `&&` / `|` -- so a `paste:@-` payload whose *content* documents
one of these forms is not refused. `echo`/`printf` redirects are not covered.
