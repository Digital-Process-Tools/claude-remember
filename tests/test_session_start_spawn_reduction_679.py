"""Spawn-reduction items 2-4 for #679 (part of #660's warm-path residue).

Item 5 (the dispatcher watchdog's per-second `sleep` poll) was ATTEMPTED and
REVERTED: collapsing it to a single `sleep "$_budget"` is correct in
isolation, but the full suite caught a real regression
(tests/test_path_resolution.py::TestMarketplacePathResolution::
test_marketplace_hooks_d_dispatches_from_plugin timing out) that no
targeted unit test surfaced. A `kill "$_wpid"` sent to the watchdog
SUBSHELL while it is asleep terminates the subshell itself almost
instantly, but does NOT kill the `sleep` PROCESS it forked to do the
sleeping -- that child is orphaned, inherits whatever file descriptors the
subshell had open (session-start-hook.sh's own promo/buffer logic saves
the real stdout as fd 3 for part of the run), and keeps running for up to
the FULL remaining budget with that fd still open, holding a test
harness's stdout pipe open long after the script itself has exited. The
OLD per-second poll loop never surfaced this because it self-terminates
within about a second of the hook finishing, on its own, long before an
external `kill` is ever needed -- so the same latent fd-inheritance never
got the time to matter. Left as-is; see this issue's report for the full
finding.

Item 1's own finding lives in test_session_start_windows_benchmark_669.py's
`_path_without_jq` docstring: both #668 caches were already hitting
correctly on a warm start; the "miss" the issue reported was that helper's
own PATH-stripping silently taking `mktemp` down with it on any machine
where jq shares a PATH directory with core tools (macOS 15+'s /usr/bin,
alongside mktemp/cat/wc). This file is items 2-4: real, independent spawn
reductions on the SAME warm path, verified against the actual shipped
scripts via the same spawn-log harness the #669 benchmark uses.

TDD shape throughout: a positive control proves the harness can see the
spawn/behaviour being removed, paired with the actual assertion that it is
gone -- CLAUDE.md's own rule for a "must not fire" case.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from . import test_session_start_windows_benchmark_669 as bench
from ._bash_runner import resolve_bash
from .spawn_counting import make_shim_dir

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found -- these tests drive the real hook scripts",
)

# The three harness-driven tests below assert on coreutils spawns (sed, cat,
# rm) that the shim directory must be able to intercept. On windows-latest
# it cannot: resolve_bash() returns Git for Windows' `Git/bin/bash.exe`,
# a launcher that prepends `/mingw64/bin:/usr/bin` to PATH before the
# script runs, so every MSYS coreutil resolves to the real binary ahead of
# the shim dir and is invisible to the spawn log -- only tools outside the
# Git install (jq, python) get counted there. Observed on CI (windows-latest
# 3.9, PR #681): the warm spawn list was three `jq` calls and nothing else,
# and the positive control ("at least one rm naming these files") failed.
# That is a harness blind spot, not evidence about the hook; the byte-
# identity test below has no such dependency and still runs everywhere.
_needs_coreutils_shims = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Git/bin/bash.exe prepends /usr/bin ahead of the shim dir, so "
    "coreutils spawns are not observable on Windows (#679)",
)


def _warm_spawns(tmp_path: Path, jq_present: bool = True) -> list[str]:
    """One cold + one warm run of the real session-start-hook.sh, same
    fixture/harness as the #669 benchmark, returning the WARM run's spawn
    list -- the state every #668 cache should already be hot for."""
    home, project, remember = bench._store(tmp_path)
    base_path = (
        os.environ["PATH"] if jq_present
        else bench._path_without_jq(os.environ["PATH"], tmp_path)
    )
    env = bench._env(home, project, remember, base_path)
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    if not jq_present:
        jq_shim = shims / "jq"
        if jq_shim.exists():
            jq_shim.unlink()
    cold, _, cold_spawns = bench._run_once(env, shims, log)
    assert cold.returncode == 0, cold.stderr[-2000:]
    assert cold_spawns, "positive control: cold run produced no spawns at all"
    warm, _, warm_spawns = bench._run_once(env, shims, log)
    assert warm.returncode == 0, warm.stderr[-2000:]
    return warm_spawns


# ── Item 4: `trap -p EXIT | sed ...`  ->  parameter expansion, zero exec ───

@_needs_coreutils_shims
def test_no_sed_spawn_recovering_the_exit_trap_679(tmp_path):
    """lib-memory-dir.sh and bootstrap-dirs.sh each chained onto the
    caller's existing EXIT trap via `trap -p EXIT | sed "..."` -- one `sed`
    fork apiece, every single invocation, to strip four characters (and,
    in bootstrap-dirs.sh's case, collapse `trap -p`'s own re-quoting of an
    embedded `'`) that parameter expansion does with zero forks.
    """
    warm = _warm_spawns(tmp_path)
    sed_trap_calls = [s for s in warm if s.startswith("sed") and "trap --" in s]
    assert not sed_trap_calls, (
        f"a warm SessionStart still forks sed to recover the EXIT trap: "
        f"{sed_trap_calls}"
    )


# ── Item 2 (partial): small fixed files read via $(<file), not `cat` ──────

@_needs_coreutils_shims
def test_no_cat_of_capture_alive_or_gap_reported_679(tmp_path):
    """`SEEN_ID=$(cat "$CAPTURE_ALIVE")` and
    `REPORTED_ID=$(cat "$CAPTURE_REPORTED")` each forked a `cat` to read a
    one-line id out of a file already about to be read into a shell
    variable -- `$(<file)`'s own builtin form does the identical read with
    no fork.
    """
    warm = _warm_spawns(tmp_path)
    offending = [
        s for s in warm
        if s.startswith("cat ") and ("capture-alive" in s or "capture-gap-reported" in s)
    ]
    assert not offending, f"still forking cat for a small id file: {offending}"


def test_session_history_hint_read_is_byte_identical_679():
    """`cat "$PLUGIN_ROOT/prompts/session-history-hint.txt"` streamed the
    file straight to stdout; `printf '%s\\n' "$(<file)"` reproduces it with
    no fork ONLY if the file's own trailing bytes are exactly one newline
    -- proven here against the real shipped file, not assumed.
    """
    hint = REPO_ROOT / "prompts" / "session-history-hint.txt"
    old = subprocess.run([BASH, "-c", 'cat "$1"', "_", str(hint)],
                          capture_output=True, text=True, timeout=10,
                          check=False).stdout
    new = subprocess.run(
        [BASH, "-c", 'printf "%s\\n" "$(<"$1")"', "_", str(hint)],
        capture_output=True, text=True, timeout=10, check=False,
    ).stdout
    assert new == old, f"byte mismatch: {new!r} != {old!r}"


@_needs_coreutils_shims
def test_no_cat_of_session_history_hint_679(tmp_path):
    warm = _warm_spawns(tmp_path)
    offending = [s for s in warm if s.startswith("cat ") and "session-history-hint" in s]
    assert not offending, f"still forking cat for the history hint: {offending}"


# ── Item 3 (narrow): the two same-script temp-file removes at the very end
#    of session-start-hook.sh, batched into one `rm -f a b` ────────────────

@_needs_coreutils_shims
def test_hook_stdin_and_ctx_files_removed_in_one_rm_call_679(tmp_path):
    """`_hook_stdin_file` and `_REMEMBER_CTX_FILE` were each removed by
    their own standalone `rm -f`, ~40 lines apart, even though nothing
    between the ctx-file's last read and the end of the script needs
    `_hook_stdin_file` to still exist -- deferring its removal that far
    costs nothing and lets both go in one call.
    """
    warm = _warm_spawns(tmp_path)
    rm_calls_naming_either = [
        s for s in warm
        if s.startswith("rm -f") and ("session-start-stdin" in s or "session-start-ctx" in s)
    ]
    assert rm_calls_naming_either, (
        "positive control: expected to see at least one rm naming these "
        f"files and saw none: {warm}"
    )
    assert len(rm_calls_naming_either) == 1, (
        f"session-start-stdin and session-start-ctx are still removed in "
        f"separate rm calls: {rm_calls_naming_either}"
    )
    combined = rm_calls_naming_either[0]
    assert "session-start-stdin" in combined and "session-start-ctx" in combined, (
        f"only one of the two temp files is named in the single rm call: {combined}"
    )
