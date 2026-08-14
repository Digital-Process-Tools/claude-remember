---
title: ".oss.json is config, not truth"
match: \.oss\.json
---

Per-repo settings for the maintainer loop: `repo`, `default_branch`, `clone`, `worktree_root`,
`test_command`, `version_sites`, `labels`, `ci.required_checks`, `state_file`, `release`.

**Re-derive anything load-bearing before acting on it.** This file records what a probe observed on
the day it ran. Two values rot first:

- **`ci.required_checks`** is the merge gate's arithmetic. Read it off the pull request every time.
  Any leg that is not a success gets named before merging -- cancelled, skipped, timed out and
  neutral are none of them passes and none of them pendings.
- **`labels`** are spellings, and they differ between repos: one spells it `priority-high`, another
  `priority:high`. Read them off the repo before writing one, and never invent a label that is not
  already there.

**`null` is an answer, not a gap.** `test_command` and `changelog_dir` may be null and mean "the
probe could not tell". Everything else null is a hole -- the probe found nothing and said nothing.

**No key here holds a credential.** The file is committed; tokens live in the forge CLI's own auth.
