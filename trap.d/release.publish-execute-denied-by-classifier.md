Tried to publish the GitHub release object for v0.26.0 by running
`scripts/release_publish.py --repo <clone> --version 0.26.0 --tag v0.26.0 --execute
--json`. Denied three times by Claude Code's own auto-mode permission classifier, in two
different agent contexts, on 2026-09-04:

1. Twice inside `oss:releaser`, back to back. That agent's own denial protocol treats one
   retry of the identical call as a legitimate probe and hands the second denial over
   rather than rewording or looping, so it stopped there and reported
   `RELEASE: released` with the release object explicitly NOT published.
2. Once more from the scheduler session (`/oss:tick`'s own context), running the exact
   command the releaser had named as outstanding.

The denial lands *before* `gh` or `release_publish.py`'s own logic runs, so it is never
one of that script's four outcomes -- not `create`, `skipped`, `could-not-run` or
`role-forbidden`. Nothing in the release machinery reports it, because nothing in the
release machinery is reached. `gh release view v0.26.0` returned `release not found`
afterwards, confirming the tag stood alone.

Cost: v0.26.0 sat tagged-but-unreleased on the remote. Every prior tag in this repo has a
release object, and a tag without one is the exact state Florian flagged as "forgotten"
for v0.21.0. The loop cannot finish a release on its own while this holds; it can only get
as far as tag, version-site bumps and the changelog fold, then hand the human a command.

Resolution that worked: Florian ran the identical command himself in-session with the `!`
prefix. It returned `state: created`, `latest: true`, and invalidated the statusline cache
-- i.e. the command was correct all along and only the classifier stood between the loop
and a finished release.

Confirmed how: three denials across two independent agent contexts, with the successful
human run of the byte-identical command immediately afterwards as the positive control.
That the command is right is observed, not reasoned.

Unsure whether the denial is deterministic for this command. Evidence against assuming so:
in the same window, a `ScheduleWakeup` call was denied once and then an identically-shaped
call succeeded roughly seven minutes later, with nothing changed in between -- so at least
some denials in this session were transient rather than rule-based. Nobody has yet checked
whether a Bash permission rule in settings covering `release_publish.py` clears this, or
which property of the command the classifier objects to (the `--execute` flag, the
`gh release create` it shells out to, or the plugin-cache path it runs from). Worth
establishing before the next release rather than during it: a release that stalls at the
last step costs a human round-trip at exactly the moment the loop looks finished.

Logged rather than promoted to a jit-context rule -- this is a harness-permissions fact
about the environment, not a fact about this repo's code, and whether it belongs in
jit-context at all is a curation call.
