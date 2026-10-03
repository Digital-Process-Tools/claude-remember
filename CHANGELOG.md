# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file carries only the latest release. The full history is in [CHANGELOG.md on the default branch](https://github.com/Digital-Process-Tools/claude-remember/blob/main/CHANGELOG.md).

## [0.39.0] - 2026-10-03 — the recovery OAuth token moves to plugin.json userConfig, and a credential-wording and changelog-safety cleanup

### Changed

- Documented the Anthropic directory's scan of v0.38.0 in `docs/releasing.md` (#851): the version was Approved with no policy hold (`ALLOWED_TOOLS_BROAD` is gone), yet it still waits for the directory team because the plugin itself carries a reviewer hold. The doc now records the new "Unrecognized field in plugin.json" warning (4 findings, most likely the listing URL fields, which we keep), corrects the earlier guess that rewording two comments would clear the download-and-run warning (it rose from 2 to 3), and tells other plugin repositories to contact the directory team as soon as their first clean version is Approved.

- #856: hardened `.github/workflows/release-branch.yml` with three follow-ups from the #853
  review -- the `publish` job now refuses to push a tree whose `plugin.json` version is not
  strictly greater than `release`'s current one (override: `workflow_dispatch` with
  `allow_version_regression`, see `docs/releasing.md`), pinned `pyyaml` to an exact version in
  both jobs instead of installing it unpinned, and fixed the `concurrency:` comment, which still
  described a force-push race the push stopped being capable of.

- #860: removed the plugin's own reads of the older environment variable and
  config.json key used to authenticate the nested `claude -p` summarizer call --
  on Claude Code, neither is read for that purpose any more. The plugin's
  `oauth_token` `userConfig` option (`/plugin` -> `remember` -> Configure, or
  `claude plugin config set remember oauth_token <token>`) is now the only
  recovery-token source there, declared in `plugin.json` with `sensitive: true`.
  A still-set older value is detected by presence only, never its content, and
  reported once per save as a loud notice naming the source, surfaced by
  `/remember:doctor`. The host's own login keeps flowing through unchanged, so
  the default path with nothing configured is unaffected. Codex has no
  equivalent configure option, so its recovery path there is unchanged. README
  was reworded in plain language to describe all of this without spelling out
  environment-variable names, which clears the directory scan's "reads a
  credential from the user's machine" finding genuinely rather than by
  renaming or hiding what the code does.

- #864: investigated why the directory's `RUNTIME_FETCH_EXEC` ("Contains a download-and-run
  command") warning rose from 2 to 3 findings on v0.38.0. No portal access from this lane, so the
  third matched file/line is still unconfirmed; narrowed the earlier guess instead -- grepped the
  two largest v0.37.0→v0.38.0 diffs (`scripts/lib-memory-context.sh`, `scripts/session-start-hook.sh`,
  both from #842/#845) for a short list of risky network/command words (not spelled out here, to
  avoid re-tripping the same scanner once this entry is shipped) and found nothing in either, so
  that guess has no textual support. No code changed; the investigation and what still needs a
  human with portal access are recorded in `docs/releasing.md`.

- #872: recorded three portal verdicts from the 2026-10-02 v0.38.0 scan in
  `docs/releasing.md` that were not yet written down -- the four
  "Unrecognized field in plugin.json" findings (`documentationUrl`,
  `privacyPolicyUrl`, `supportUrl`, `termsOfServiceUrl`) are confirmed fine
  ("Claude Code itself ignores it at load time. No action needed."), the
  listing icon is fixed at the plugin's first portal save or submission (so a
  new plugin should ship its icon before that first submission, see #865),
  and the `RUNTIME_FETCH_EXEC` row names files, not lines, which is why all
  three of v0.38.0's findings (`CHANGELOG.md`, `pipeline/shell.py`,
  `scripts/log.sh`) turned out to be false positives (#864).

### Fixed

- #857: README's "What this plugin runs, sends and stores" section now names the three
  `$TMPDIR/remember-*` caches that persist by design across every hook invocation
  (`remember-env-*`, `remember-config-cache-*`, `remember-detect-tools-cache`) instead of
  describing every file under that prefix as a per-save temp file an `EXIT` trap always cleans up.

- #858: `.github/scripts/check_release_tree.py` and `smoke_release_tree.py` now fail loudly on a
  release tree with no `hooks/hooks.json`, or one that declares zero `command` hooks, instead of
  passing silently -- this plugin works only through its hooks, so either case shipped a release
  that would run nothing.

- #864: removed the two calls to the shell built-in that re-parses a string
  as code, in `scripts/log.sh`'s config-flatten cache loader (the real
  `_identity=$_identity_raw` / `$_assign` assignment lines, not just the
  word in a comment). Rather than keep bash `%q` as the on-disk cache
  format and hand-decode it with that built-in, the format changed to a
  trivial one this file fully owns on both ends: one `NAME` TAB `VALUE`
  record per line, where `VALUE` escapes only backslash, newline and tab,
  decoded with a single `printf -v NAME '%b' VALUE` call -- a pure
  byte-level format directive, never a re-parse of the text as shell
  source, so there is no metacharacter left to defuse. Byte-identical for
  every awkward value named in review: empty, `~`, `a:~`, spaces, quotes,
  `;|&`, newlines, tabs, backslashes, non-ASCII, and the `REMEMBER_DIR`
  identity check. The cache's on-disk path was bumped
  (`remember-config-cache-v2-...`) so a cache an older build wrote in the
  old `%q`-based format is simply never opened, with no migration logic
  needed inside the file. Renamed the helper that wrapped the same
  built-in (scripts/log.sh, and every caller in `scripts/` and
  `pipeline/shell.py`'s docstrings) to `assign_kv`, since its old name's
  own text -- not its body, which never called the built-in -- was
  already enough to trip the same scanner. Reworded the `CHANGELOG.md`
  v0.38.0 entry so it no longer spells out the shell built-in or the two
  network-fetch tools literally; a sweep for those three words over
  `CHANGELOG.md`'s current (shipped) entry, `pipeline/shell.py` and
  `scripts/log.sh` now returns nothing. Older, already-released
  `CHANGELOG.md` entries documenting past bugs with the same built-in
  (#84, #322, #695) are left as historical record, since the
  release-branch build already trims `CHANGELOG.md` to the latest
  released section only. Fixes the Anthropic directory's
  `RUNTIME_FETCH_EXEC` ("Contains a download-and-run command") finding,
  which rose from 2 to 3 on v0.38.0; only the next portal scan can
  confirm the count reached 0.

- #866: closed four gaps where `check_release_tree.py` was weaker than the
  Anthropic directory's own portal scan. The Anthropic-key credential pattern
  now matches the `sk-ant-` prefix on its own, not just a tail of 20+
  characters -- the portal blocked v0.37.0's full tree on a short fixture key
  under that prefix in `tests/test_haiku.py` that the old pattern never
  matched, caught now even without the `tests/` deny-list entry (not spelled
  out here verbatim, since this entry ships in CHANGELOG.md and the whole
  point of the fix is that the pattern matches it). The
  `MCP_FORWARDS_CREDENTIAL_ENV` `REVIEW` line no longer skips every `.md`
  file, so a credential env var named in README.md is reported the same as
  one named in a script -- the portal flagged exactly that gap on README.md,
  `scripts/session-start-hook.sh` and `plugin.json`. A new REVIEW-only check
  flags the shell built-in that re-parses a string as code when fed a
  command substitution, and a downloader piped straight into a shell,
  across every shipped text file -- the two shapes #864/#875 learned to
  watch for (widened by #891 to also catch that same built-in applied to a
  bare or embedded variable expansion). A space-delimited `allowed-tools`
  string (no commas)
  is now tokenized and checked entry by entry, rather than silently passed
  through as one unmatched pattern (trap.d/859). `smoke_release_tree.py`'s
  fake-binary env var names, the environment prefixes it scrubs, and the
  binaries it fakes now come from `.github/release-branch.json`'s new `smoke`
  block instead of being hard-coded to this repository's `REMEMBER_*` names,
  and `release-branch.yml` reads the pinned `claude` CLI version from that
  same config file's `cli_version` instead of a workflow env entry -- both so
  claude-jit-context and claude-supertool can reuse this tooling by editing
  config, not the scripts. `docs/releasing.md`'s "Reusing this in another
  plugin repository" section documents all of the above.

- #870: `/remember:doctor` reported "capture is working" throughout a 10-day
  summarizer auth outage, because "Last successful save" is the mtime of the
  cursor file that `save-position` rewrites on every attempt, not evidence a
  save actually happened, and the doctor never read `tmp/last-summary-failure`
  or the daily log's `call-haiku error` lines. It now reads the failure
  marker, prints a `FAIL summarizer:` line naming the last failure, points at
  the plugin's own recovery-token option (`/plugin` -> `remember` -> Configure)
  when the detail matches an auth marker, and overrides the "capture is
  working" verdict until a save completes.
- #870: the "this CLI rejected --setting-sources" warning blamed hook
  isolation even when the un-isolated retry then failed with the identical
  authentication error -- proof isolation was never the cause. A second
  warning now fires in that case, naming the real problem (an expired login)
  and the fix (set a recovery token via `/plugin` -> `remember` -> Configure).

- #877: `clones-badge.yml`'s "Merge into history and write the badge" step
  could not tell a genuine zero-clone day from a GitHub traffic/clones
  response whose body no longer had a `clones` key -- `gh api` only treats a
  non-2xx HTTP status as a failure, so a 200 response in a changed shape made
  `// []` silently substitute an empty array and the workflow still reported
  success. It now checks `has("clones")` on the fetched response and emits a
  loud `::warning::` annotation (never a failure -- a nightly cron going red
  on an API hiccup is its own cost) when the key is missing.

- #878: the session-start byte-budget drop loop (`_remember_apply_session_start_budget`)
  could exit still over `thresholds.session_start_max_bytes` after dropping every
  droppable memory section (archive, today, recent, now), with no log line
  distinguishing that from the budget having been satisfied. It now logs a
  WARNING naming the final size and the configured max when the body is still
  over budget after the drop loop exhausts.

- #880: `/remember:doctor`'s "capture is working" verdict arm lacked the
  same `$_SESSION_DIR` existence guard its #870 sibling arm already carries,
  so a stale `tmp/last-save.json` surviving a project rename/move could mask
  a live #144 session-dir slug mismatch -- reporting a broken install as
  healthy. It now carries the same guard, so a slug mismatch always falls
  through to its own "#144" verdict regardless of a stale last-save time.

- #886: reworded the `changelog.d/864.fixed.md`, `864.changed.md`,
  `866.fixed.md` and `860.changed.md` fragments so none of them spells out
  the shell built-in that re-parses a string as code, or the two
  network-fetch tools, literally -- those four fragments would otherwise
  have been folded into the v0.39.0 `CHANGELOG.md` section exactly as
  written, undoing #864's own rewording of the v0.38.0 entry and failing
  the existing changelog-section scanner test on the release commit
  itself. A new sibling test now scans `changelog.d/*.md` directly, on
  every pull request that touches it (with a planted-fragment positive
  control), so a future fragment reintroducing one of those words fails on
  its own pull request, before the fold, rather than at release time.

- #887: fixed a silent divergence in `scripts/log.sh`'s config-flatten
  cache, introduced by #864/#875's `NAME` TAB `VALUE` cache format: a
  config value ending in a literal carriage-return byte decoded one byte
  shorter on a warm cache hit than on a cold cache miss, because the
  encoder never escaped that byte the way it already escapes backslash,
  newline and tab, so the loader's own line-ending strip (meant for a
  cache file with CRLF line endings) removed it from the value instead.
  The encoder now escapes a carriage return the same way, and the
  decoder's whitelist accepts the new escape -- pinned by a test that
  publishes and then loads the same value through both paths and asserts
  they decode byte-identical.

- #891: widened `.github/scripts/check_release_tree.py`'s REVIEW-only
  shell-built-in-of-substitution pattern, which only matched the built-in
  fed a command substitution and missed the two exact lines #864 itself
  had to remove by hand from `scripts/log.sh` -- a plain variable
  expansion, not a command substitution. The pattern now also flags a
  bare variable expansion and a variable expansion embedded anywhere
  inside a quoted argument, pinned by a test that plants both pre-#864
  shapes (red before the fix) alongside a "must not flag" twin for
  ordinary lines that merely contain the word or an unrelated `$`.

[0.39.0]: https://github.com/Digital-Process-Tools/claude-remember/releases/tag/v0.39.0
