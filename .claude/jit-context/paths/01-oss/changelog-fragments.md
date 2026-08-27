---
title: "Changelog fragments"
description: "One file per pull request; do not hand-edit CHANGELOG.md while changelog.d/ exists -- the fold overwrites it and deletes the fragments."
match: (changelog.d/|(^|/)CHANGELOG\.md$)
---

One file per pull request, so two open PRs never touch the same file. `CHANGELOG.md` is assembled
from these at release time and the fragments are deleted.

**Name:** `<issue>.<section>[.<slug>].md`, where the section is a Keep a Changelog heading,
lowercased: `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`. `<slug>` is optional
and lets one issue file two entries in one section without two pull requests colliding on a path.

**Body:** a single top-level `-` list. No headings, no raw HTML, no unclosed fences. Name the issue
in the text as well as the filename -- the filename is metadata, and metadata does not survive being
read out of context.

**A `removed` fragment must declare compatibility**, as one more bullet in that list:

    - Compatibility: breaking - <reason>
    - Compatibility: compatible - <reason>

`/oss:release` reads it to propose the version. A removal that declares nothing stops the proposal
and names the file, rather than being read as a quiet minor -- whether a removal breaks anything is
the question the number turns on, and an author who knows the answer and writes it as prose puts it
where nothing can read it. The reason is part of the field: a bare verdict is the same unsourced
answer one field further along. Other sections may carry the bullet and are read as compatible when
they do not.

**Do not hand-edit `CHANGELOG.md`** while this directory exists. The fold overwrites it and deletes
the fragments; an entry written directly into the file is lost at the next release, silently,
because the fold has no way to know it was meant to stay.

Check before pushing:

```bash
python3 .oss/assemble_changelog.py --check --check-links --dir 'changelog.d' --changelog CHANGELOG.md
```

`--check-links` refuses when a `## [x.y.z]` section has no link reference definition. If the
version it names was never tagged, the missing link is the correct state: there is no release
page to point at, and a `releases/tag/vX.Y.Z` URL written for one is a 404 that renders as a
working link.

This repository declares no untagged versions in `.oss.json`, so every `## [x.y.z]`
section is expected to carry a link ref. If one of them was never tagged, add it to
`changelog_untagged` and re-run `/oss:scaffold --apply` — the CI leg reads the same key,
so the two cannot disagree. Declaring `[]` says the same thing deliberately.
