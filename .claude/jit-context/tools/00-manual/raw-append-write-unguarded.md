---
title: "supertool's raw-command guard blocks a raw read, not a raw redirect-append write"
description: "A raw printf '...' >> file ran unvalidated, with no receipt and no rollback, while the equivalent cat/sed/head/tail/grep-shaped read at command position is refused. Filed upstream (claude-supertool#2699); use edit:@- / paste:@- for any write."
tool: Bash
match: ~>>[[:space:]]*[^[:space:];&|]+
mode: remind
---

Only read-shaped commands (cat/sed/head/tail/grep) are intercepted by the raw-command guard at
command position. A raw `printf '...' >> FILE` append is not: it runs with no jsonlint/ruff/
gitleaks validation and no rollback, silently, the same gap `python-heredoc-writes-are-unvalidated.md`
already covers for `cat > FILE <<EOF` and a `python3 -` heredoc write. **The guard's silence on a
`>>` append is not proof the write is safe** -- it means no receipt exists for it at all. Route any
write, append included, through `supertool 'edit:@-'` or `supertool 'paste:@-'` instead of a raw
redirect.

**Why this lives here and not beside the shipped supertool rules:** filed upstream as
[claude-supertool#2699](https://github.com/Digital-Process-Tools/claude-supertool/issues/2699) --
delete this file once guard coverage ships there. Until then this is an interim, human-written
copy; `01-oss/` is generated and replaced wholesale on every plugin install, so an append there
would be destroyed silently by the next update.
