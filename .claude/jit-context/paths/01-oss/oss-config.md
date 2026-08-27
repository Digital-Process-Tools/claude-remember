---
title: ".oss.json is config, not truth"
description: "Per-repo settings for the maintainer loop. Re-derive labels before acting; the CI leg count is not in here; null is an answer, not a gap."
match: \.oss\.json
---

Per-repo settings for the maintainer loop: `repo`, `default_branch`, `clone`, `worktree_root`,
`test_command`, `version_sites`, `labels`, `state_file`, `release`.

**Re-derive anything load-bearing before acting on it.** This file records what a probe observed on
the day it ran. `labels` rots first: they are spellings, and they differ between repos -- one spells
it `priority-high`, another `priority:high`. Read them off the repo before writing one, and never
invent a label that is not already there.

**The CI leg count is not a key here, and must not be added as one.** There was a
`ci.required_checks`; it counted workflow job declarations, which a build matrix, a reusable
workflow or an organisation/app-level check multiplies or adds to invisibly, so the number on disk
was never the merge gate's. Count the legs on the pull request they apply to, every time. Any leg
that is not a success gets named before merging -- cancelled, skipped, timed out and neutral are
none of them passes and none of them pendings. A config still carrying the block is harmless and
safe to delete; nothing reads it.

**`null` is an answer, not a gap.** `test_command` and `changelog_dir` may be null and mean "the
probe could not tell". Everything else null is a hole -- the probe found nothing and said nothing.

**`changelog_untagged` has three states and they are three.** It lists the `## [x.y.z]` sections in
`CHANGELOG.md` that were never tagged, so the link-ref audit does not demand a `releases/tag/v...`
URL that would 404. Absent or `null` means nobody declared anything and every section is expected to
carry a link ref -- a default reading, not a statement. `[]` means the repository has declared that
every section was tagged: the same audit, and a decision on record. A list names the exempt versions.
The scaffolded CI leg and the fragment rule both render from this key, so the answer is written once.
Versions, not tags: `0.1.0`, never `v0.1.0`. A declared version with no matching section is a finding.

**No key here holds a credential.** The file is committed; tokens live in the forge CLI's own auth.
