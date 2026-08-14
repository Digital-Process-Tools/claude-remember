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
<issue>.<section>.md
```

`<section>` is a Keep a Changelog heading, lowercased: `added`, `changed`,
`deprecated`, `removed`, `fixed`, `security`.

## Body

A single top-level `-` list. No headings, no raw HTML, no unclosed fences. Name the
issue in the text as well as in the file name — the file name is metadata, and
metadata does not survive being read out of context.

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
