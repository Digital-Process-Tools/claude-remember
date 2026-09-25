---
title: "Simulating 'tool X absent' on macOS by dropping a PATH entry can silently take sibling tools down with it"
description: "macOS 15+ ships jq at /usr/bin/jq, the same directory as mktemp/cat/wc/sh and most of coreutils -- removing that whole PATH entry to fake jq's absence breaks every one of them for the test, and copying a system binary elsewhere to work around it gets SIGKILLed by AMFI/codesigning."
match: tests/.*\.py$
---

A harness helper that simulates "tool X absent" by removing every PATH entry containing X's
binary can remove OTHER tools too, on a platform where the vendor bundles them together. macOS
15+ ships `jq` at `/usr/bin/jq` -- Apple's own build -- in the **same directory** as `mktemp`,
`cat`, `wc`, `sh` and most of coreutils. A helper written to simulate "jq absent"
(`tests/test_session_start_windows_benchmark_669.py`'s `_path_without_jq`, before #679) dropped
the whole PATH entry, taking `mktemp` down with it for the entire test process -- silently
breaking every cache that publishes via `mktemp`, cold run included, with no signal at the point
the helper runs. It surfaced only as an unrelated-looking failure investigated first as a bug in
production code, before tracing found the harness was the actual cause -- and only on machines
where the real tool genuinely lives beside others in one directory. (A genuine Windows/Git-Bash
"jq absent" install has no jq anywhere on PATH at all, so the real hook has never had this
problem; only the local *reproduction* of "jq absent" did.)

**The fix, and a second trap inside the fix.** Rebuild the helper to remove only the target
file's reachability -- an exec-shim view directory holding every OTHER file from the same real
directory, each shimmed to `exec` at its ORIGINAL path -- rather than hiding the whole directory.
A first attempt at that used `shutil.copy2` to populate the view directory, which is ALSO wrong:
copying a macOS *system* binary out of `/usr/bin` gets it killed the instant it runs (`rc=137`,
SIGKILL) -- Apple's code-signing/AMFI enforcement ties a Mach-O binary's validity to its own
signed location, and a copy is not that location. An `exec`-shim script that points at the
ORIGINAL, still-signed path (the same shape `tests/spawn_counting.py`'s own `make_shim_dir`
already uses) sidesteps this: never `shutil.copy2` (or otherwise relocate the bytes of) a macOS
system binary out of its own directory, and never drop a whole PATH entry to simulate one tool's
absence when other real tools might share it.
