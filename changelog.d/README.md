# changelog.d/ — changelog fragments

One file per pull request, so two open pull requests never edit the same line of
`CHANGELOG.md` and stop conflicting on every merge. Fragments are folded into
`CHANGELOG.md` at release time and deleted.

This directory is created empty, before there is anything to put in it, because
`.github/workflows/oss-changelog.yml` reads it on every pull request and an absent
directory is a failure rather than an empty one. The first red build in a repository
should not be the pull request that installed the check.

## Naming

```
<issue>.<section>[.<slug>].md
```

`<section>` is a Keep a Changelog heading, lowercased: `added`, `changed`,
`deprecated`, `removed`, `fixed`, `security`. `<slug>` is optional and lets one
issue file two entries in one section without two pull requests colliding on a
path, e.g. `878.fixed.second-entry.md`.

## Body

A single top-level `-` list. No headings, no raw HTML, no unclosed fences. Name the
issue in the text as well as in the file name — the file name is metadata, and
metadata does not survive being read out of context.

## Compatibility, on a `removed` fragment

A `removed` fragment must say whether the removal breaks anything, as an ordinary
bullet in the body:

```markdown
- Compatibility: breaking|compatible - <reason>
```

The release number is proposed from these fragments, and a `removed` fragment that
declares nothing stops the proposal rather than defaulting quietly — a patch bump
over a breaking change is indistinguishable in the tag from a considered one. A word
that is neither `breaking` nor `compatible` stops it too, so a value nothing
recognises never grades as compatible.

The reason after the verdict is required: a bare flag is the same unsourced verdict
one field further along, and the sentence is the part worth having.

Only `removed` is required to carry one. Every other section may, and a fragment that
says nothing is read as compatible with the count of such fragments reported out
loud. A field on every fragment is a field on every fragment to get wrong, so it is
required exactly where the question is genuinely open.

It is a plain bullet rather than front matter, so the assembler needs no special case
and the claim ships into `CHANGELOG.md` where a user reads it, instead of being
metadata deleted at the fold.

## Nothing user-visible in this change?

Label the pull request `no-changelog`. **That label is not created for you.** Writing
a file into a checkout is a change somebody reads in a diff and reverts; creating a
label changes the repository on the forge, from a tool that was run to write files.
So it is named here instead, with the command:

```bash
gh label create no-changelog --description "Change is invisible to users"
```

Until that label exists the check has no escape hatch, and every pull request needs a
fragment.
