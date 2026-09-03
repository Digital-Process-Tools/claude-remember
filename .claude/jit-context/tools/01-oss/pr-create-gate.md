---
title: "Before gh-pr-create: don't retype what already arrived right"
description: "title, head and base arrive filled in from the report -- retyping any of them is the one move that makes things worse. Publish gh-pr-create:@FILE, never a hand-built body."
tool: Bash
match: ~gh-pr-create
mode: remind
---

Pushing and opening is one read plus one call, not a document you write:

- **Read the published body before anything else touches it.** A body you have not read
  is your name on text you did not see -- argue it or send it back, never rewrite it
  quietly, because the person who did the work writes the record.
- **Hand the payload path to `gh-pr-create:@FILE`.** Not `gh pr create`, not a body of
  your own. The op refuses a body with no working `Closes #N` -- nothing is created --
  and the error names the remedy: fix the reference, or set `no_close = true` at the
  payload's top level for a `Part of #N` pull request that closes nothing.
- **Do not retype `title`, `head` or `base`.** They arrive filled in and measured right;
  a hand-written value is the only one nothing downstream verifies at all.
- **Spend one call on the check, not the claim:**
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report_schema.py" <the report path>` -- the
  report path, never the payload path. Three answers: `ok`, a finding, and
  `UNVALIDATABLE` at exit 2, which is a statement about the validator and not the report.
- **Release what a lane did not finish.** A lane with no commit, or a pull request closed
  without merging, both leave an assignment behind: `gh issue edit <N> --remove-assignee
  @me`. *Could not read the pull request state* must never render as released.

Full argument -- the fragment-rename procedure, the three states of `pr_body`, how far
the validator actually gets on each field, and the two ways a body silently references
less than it appears to: `skills/manager/phases/handback.md`.
