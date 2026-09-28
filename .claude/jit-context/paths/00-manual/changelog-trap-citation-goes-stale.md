---
title: "Don't cite a trap.d/ path by name in changelog prose -- curate deletes it later"
description: "CHANGELOG.md permanently named two trap.d/NNN....md fragments by path; a later curate pass promoted or declined both and deleted the files, leaving the published changelog pointing at nothing."
match: (changelog\.d/|(^|/)CHANGELOG\.md$)
mode: remind
---

Confirmed twice at HEAD (#827): `CHANGELOG.md` cited
`trap.d/804.edit-op-defaulted-to-main-clone-not-worktree.md` and
`trap.d/745.entrypoint-sniff-has-two-unfixed-edges.md` by path in "Fixed" prose; a later curate
pass (`/oss:run:curate`) deleted both files once it promoted or declined them into a jit-context
rule. Neither file exists at HEAD — the changelog entry is otherwise accurate but 404s on its own
citation.

**A changelog fragment (`changelog.d/*.md`) that names the trap.d file behind a fix should
describe the mechanism in prose instead of citing the trap.d path** -- the path is provenance for
the PR that wrote the fragment, not a durable pointer: `trap.d/` is emptied by every curate pass,
on a schedule the changelog fragment's author does not control. Citing the issue number (`#NNN`)
is durable; citing the trap.d filename is not.

No referential-integrity check exists for this (`assemble_changelog.py --check` / `--check-links`
validate fragment shape and version links only, never `trap.d/`) -- this is a naming-convention
fix, not a tooling gap to route around.
