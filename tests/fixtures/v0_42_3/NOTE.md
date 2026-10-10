# v0.42.3 vendored hidden-launch assets (#1002 round 3)

These three files are byte-for-byte copies of the ORIGINAL (round-2, pre-this-round) hidden-launch
shape as it was actually shipped and tagged `v0.42.3`:

```
git show v0.42.3:scripts/lib-detach.sh          > tests/fixtures/v0_42_3/lib-detach.sh
git show v0.42.3:scripts/lib-detach-pidwrap.sh  > tests/fixtures/v0_42_3/lib-detach-pidwrap.sh
git show v0.42.3:scripts/windows-hidden-run.vbs > tests/fixtures/v0_42_3/windows-hidden-run.vbs
```

Tag `v0.42.3` resolves to commit `525e07ef93a963693135c15c159285c838c9c023` (2026-10-10). Vendored
rather than fetched with `git show v0.42.3:PATH` at test time, because `.github/workflows/tests.yml`
checks out with `actions/checkout@v7`'s default (shallow, `fetch-depth: 1`) and never fetches tags --
the existing "Fetch origin/main" step only fetches that one branch, `--no-tags`, at depth 1 -- so a
`v0.42.3` ref would not resolve in the CI job that is the only place these tests can actually run
(a real Windows host with real Windows Script Host).

Why these three files exist here at all: `tests/test_windows_real_wscript_v0_42_3_1002.py` (#1002
round 3, maintainer instruction) drives the ORIGINAL shipped shape -- the one every v0.42.3 install
actually runs -- against a real `wscript.exe`, something no previous round ever did. Round 2 fixed
the code based on two REASONED-only claims (trap.d/1002.detach-windows-silent-skip-after-launch.md,
trap.d/1002.wsh-strips-quotes-in-hidden-launch-c-script.md) and then observed the NEW shape working
on real Windows Script Host -- but never ran the OLD, shipped v0.42.3 shape against real WSH, so
whether v0.42.3 itself was actually broken was never settled by observation. This vendors the exact
bytes a v0.42.3 install runs so that question can finally be answered from a real CI job rather than
reasoned about.

These files are NEVER sourced by anything under `scripts/` and are NEVER edited to track later
fixes -- editing them would defeat the entire point, which is to keep testing the EXACT bytes a
`v0.42.3` install already shipped. If a future round needs evidence against a *later* tag, vendor a
*new* directory (e.g. `tests/fixtures/v0_43_0/`) rather than overwriting this one.
