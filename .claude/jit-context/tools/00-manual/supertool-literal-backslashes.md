---
title: "A single `\\n` you mean to reach disk: literal_backslashes"
description: "edit:@- refuses an even run of backslashes in a written field. Writing Python source containing '\\n' trips it legitimately; the refusal names the flag that allows it."
tool: Bash
match: ~supertool.*edit:@
mode: remind
---

`edit:@-` with a TOML `'''` literal block refuses when a **written** field carries an even run of
backslashes, because that is the shape of a copy/paste that got doubled:

```
ERROR: @file payload refused (<stdin>): a ''' literal block carries an EVEN run of
backslashes (\\, \\\\, ...) in a field that is WRITTEN to the file ...
```

Writing *Python source* that itself contains `'{"type": "x"}\n' * 51` trips this legitimately: the
two characters `\` and `n` genuinely belong on disk, so the target file parses them as an escape.
The guard cannot tell that from the bug class it exists to catch (#543).

The refusal names its own fix. Use it rather than routing around the write path:

```toml
literal_backslashes = true          # or: literal_backslashes = ["new"]
```

Observed cost of not trusting it: a lane fell back to a raw `python3 - <<'PYEOF'` heredoc for one
40-line edit, bypassing supertool's post-write validators (ruff, git-status, py-syntax) entirely,
then re-ran ruff and pytest by hand to compensate. The flag was never tried.

**Why this lives in `00-manual/` and not beside the shipped supertool rules:** `01-oss/` is
generated and replaced wholesale on every plugin install (`oss_rules.py`), so an append there is
destroyed silently by the next update. The durable home for this is upstream in the plugin that
owns the op; until it lands there, it lives here.
