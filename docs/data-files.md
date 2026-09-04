# Data files

Moved verbatim from the README.

The pipeline writes to `REMEMBER_DIR` (created automatically). By default this is `.remember/` inside your project root; in external storage mode it is a per-project subdirectory of `~/.remember/` (see [External storage mode](external-storage-mode.md)).

| File                           | Purpose                                           |
| ------------------------------ | ------------------------------------------------- |
| `now.md`                       | Current session buffer                            |
| `today-*.md`                   | Daily compressed summaries                        |
| `recent.md`                    | Last 7 days consolidated                          |
| `archive.md`                   | Older history consolidated                        |
| `archive-YYYY-MM-DD.md`        | Rotated archive slices — searchable, not auto-loaded |
| `recent-YYYY-MM-DD.md`         | Rotated `recent.md` spans — searchable, not auto-loaded |
| `remember.md`                  | Handoff note written by `/remember` (`handoff_mode: "single"`, the default) |
| `remember.<session_id>.md`     | Per-session handoff note (`handoff_mode: "per_session"`, [#363](https://github.com/Digital-Process-Tools/claude-remember/issues/363)) — not pruned automatically |
| `logs/`                        | Pipeline logs — local to this machine, never backed up |
| `tmp/`                         | Lock files, cooldown markers, handoff delivery record, this session's [slug record](computing-the-slug-outside-bash.md#1-read-the-slug-this-session-computed), each invocation's merged config — local to this machine, never backed up |
| `identity.md`                  | Per-project identity override (optional)          |
| `.claude/remember/identity.md` | Your agent's identity and values (you write this) |

In [external storage mode](external-storage-mode.md) with `{slug}` in `data_dir` there is one more file, and it is **not** inside `REMEMBER_DIR`: `<store root>/tmp/sessions`, the [session index](computing-the-slug-outside-bash.md#2-find-the-record-when-the-slug-names-its-directory). It is per-machine state like the rest of `tmp/`, excluded from the git backup, and it exists because that is the one place a non-bash caller can name without already knowing the slug.

**`tmp/remember-config-<pid>.json`** is the three-layer config merge (bundled defaults, `~/.remember/config.json`, this project's `config.json`) for one invocation. It is created and removed by the same process, via an `EXIT` trap — and on Windows/Git Bash that trap does not reliably fire for this plugin's short-lived hook processes, so one leaked, unremoved copy per hook call was observed accumulating directly in the OS temp directory (23,908 of them in one report, [#362](https://github.com/Digital-Process-Tools/claude-remember/issues/362)). Since #362 the file lives here — a directory this plugin owns, rather than one shared with every other app on the machine — and every invocation also sweeps away any copy here whose age says its own process is long gone, so a trap that never fires no longer leaks forever.
