# .oss/ — files the oss plugin owns

Everything in this directory is **ours**: written by `/oss:scaffold` and **replaced
wholesale** on every run — an edit here is **overwritten** at the next update.

The plugin distinguishes three kinds of file in your repository, and that distinction is
why this directory exists at all:

| Kind | Where | On update |
| --- | --- | --- |
| **Yours** | everywhere else | never read, never written |
| **Defaults** | `SECURITY.md`, `CLAUDE.md`, `.github/ISSUE_TEMPLATE/`, … | created once when absent, then yours forever — never overwritten |
| **Ours** | this directory | replaced every time, so fixes actually reach you |

To change something here, copy it out and point your own config at the copy.

## The one exception

`.github/workflows/oss-changelog.yml` is ours too and is replaced the same way. It
cannot live in here: a forge reads workflows only from `.github/workflows/` itself —
subdirectories are not supported and a symlink there fails outright. So it keeps the
`oss-` prefix and carries the same note in its own header.

## What is here

Every file this directory holds, and nothing else — `/oss:scaffold` writes these and
replaces them wholesale on every run.

- `README.md` — this file. It ships beside the others because a directory of generated
  files with no note is a directory somebody edits.
- `assemble_changelog.py` — validates changelog fragments and folds them into
  `CHANGELOG.md` at release time. It lives in your repository rather than in the plugin
  because CI checks out your repository and nothing else.
- `statusline.py` — renders one status line for this repository: the tracker board, when
  the next tick is due, and whether the plugin copies you are running are current. It is
  **opt-in, and nothing here calls it**: it stays inert until a `statusLine` entry in
  `.claude/settings.json` points at it, so removing that entry stops it and breaks nothing else.
  The command that entry runs is

  ```bash
  python3 "$CLAUDE_PROJECT_DIR"/.oss/statusline.py
  ```

  `/oss:scaffold` writes that entry only when the file has no `statusLine` at all; one you
  already set is never touched, whatever it points at. Every field it prints has three
  states and the third is `?` — a count nobody took, or a version comparison nobody could
  make. `?` is never rounded up to `0` or to a tick.

## Running the fragment check yourself

It parses each fragment with markdown-it-py and refuses to fall back to scanning the text
when that is missing — it reports `skipped` and claims nothing. The generated workflow
installs it; your machine is not covered by that, so before pushing:

```bash
python3 -m pip install markdown-it-py
python3 .oss/assemble_changelog.py --check --dir 'changelog.d' --changelog CHANGELOG.md
```
