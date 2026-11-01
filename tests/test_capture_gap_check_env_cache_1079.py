"""#1079: capture-gap-check.sh's fast path re-ran config bootstrap.

Before this fix, the fast path (inherited PROJECT_DIR/PIPELINE_DIR/REMEMBER_DIR,
#1069) unconditionally sourced log.sh, which re-ran lib-memory-dir.sh's whole
three-layer config merge -- 2-6 extra spawns measured on macOS, plus a
#362-style mktemp leak into shared OS tmp, on EVERY SessionStart. The fix loads
the env cache session-start-hook.sh already publishes (REMEMBER_RECOVERY_ENABLED,
alongside the two scalars post-tool-hook.sh's own fast path already replays)
instead of re-running the merge, with a lazy log() for the one rare call site
(case-divergence) that still needs it.

Measured with `tests/spawn_counting.py`'s PATH-shim harness, NOT `bash -x`:
`source log.sh 2>/dev/null` redirects stderr for the whole sourced chain, which
hides every xtrace line (and therefore every fork) a trace-grepping test would
have tried to see -- a `bash -x` harness cannot tell "the merge ran" from "the
merge's own trace was swallowed by the redirect" and silently reports a false
pass either way. The shim harness counts real executions instead, so it sees
through the redirect.

TDD shape: a positive control (cache missing, AND log.sh's own separate #668
flatten-cache also missing -- both must be cold to force the real merge) is
paired with the actual claim (cache warm -> zero mktemp/jq from the merge), the
same "must not fire" / "must fire" pairing this repo's own CLAUDE.md requires
for a negative assertion.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.slug import session_dir_slug as _slug

from ._bash_runner import decode_bash_output, resolve_bash
from .spawn_counting import make_shim_dir, spawns

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found -- these tests drive the real hook scripts",
)

SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
CAPTURE_GAP_CHECK = REPO_ROOT / "scripts" / "capture-gap-check.sh"


def _project_and_home(tmp_path: Path, *, with_user_config: bool):
    project = tmp_path / "project"
    remember = project / ".remember"
    remember.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    if with_user_config:
        (home / ".remember").mkdir(parents=True)
        cfg = home / ".remember" / "config.json"
        cfg.write_text(json.dumps({"features": {"recovery": False}}))
        # Explicit, spaced-in-the-past mtime (same convention as
        # tests/test_capture_gap_notice.py's own _transcripts helper): the
        # env cache's own `-nt` validity check works only to the SECOND
        # (bash's `-nt`), and a config file created in the same wall-clock
        # second as the cache published moments later can read as "not
        # older" and fail the comparison -- the identical class #1021/#842
        # already named for other cache-mtime checks in this repo.
        past = time.time() - 120
        os.utime(cfg, (past, past))
    return project, remember, home


def _warm_the_cache(project: Path, home: Path, systmp: Path) -> dict:
    """Run session-start-hook.sh once (REMEMBER_DEFER=0, synchronous) so it
    resolves config and publishes the env cache capture-gap-check.sh's fast
    path reads -- exactly what a real SessionStart does moments before
    dispatching the deferred phase. Unshimmed: this run's own spawns are not
    what either test below is about.
    """
    systmp.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
        "TMPDIR": str(systmp),
        "REMEMBER_DEFER": "0",
    }
    payload = json.dumps({
        "session_id": "f1079000-0000-4000-8000-000000001079",
        "transcript_path": "/does/not/matter/1079.jsonl",
        "hook_event_name": "SessionStart",
        "cwd": str(project),
        "source": "startup",
    })
    result = subprocess.run(
        [BASH, str(SESSION_START)], env=env, input=payload,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"warm run exited {result.returncode}: {result.stderr[-2000:]}"
    return env


# Same id the warm run used (#1079's own done-when criteria care about the
# ORDINARY path, where a real session id is on stdin -- a direct/standalone
# invocation with NO argument, the shape test_session_start_spawn_reduction_679.py's
# OWN helper uses to probe the resolve-paths.sh branch, instead takes the
# "no session_id on stdin" arm here and calls log() unconditionally, which
# lazily sources log.sh for its ONE rare call site -- a real finding about
# that fixture shape, not about the fast path this file is actually testing).
_SESSION_ID = "f1079000-0000-4000-8000-000000001079"


def _run_capture_gap_check_shimmed(
    project: Path, remember: Path, env: dict, tmp_path: Path,
    *, recovery_enabled: str | None = None,
) -> list[str]:
    """Run capture-gap-check.sh's fast path with every COUNTED external
    command shimmed, and return the recorded invocations. Unlike `bash -x`,
    this sees through `source log.sh 2>/dev/null`'s own stderr redirect --
    the shim records a real exec(), not a trace line.

    `recovery_enabled`, when given, is set directly into the child's own
    environment as REMEMBER_RECOVERY_ENABLED -- modelling the real #1079
    contract: session-start-hook.sh `export`s this scalar BEFORE it forks
    capture-gap-check.sh (inline or backgrounded), in the SAME bash process,
    so the child inherits it for free. This harness runs capture-gap-check.sh
    as its own, separate `subprocess.run` -- a SECOND OS process, unrelated
    to whatever `_warm_the_cache` started and already exited -- so plain
    inheritance cannot be exercised here; setting the var explicitly is the
    one way this harness can stand in for the fork it is not actually doing.
    `None` (the default) leaves it unset, modelling a direct/standalone
    invocation that has nothing to inherit -- the shape every caller of this
    helper uses for the "both caches missing" positive control.
    """
    shim_log = tmp_path / f"spawn-log-{os.getpid()}-{id(env)}"
    shims = make_shim_dir(tmp_path, shim_log)
    env2 = {
        **env,
        "PROJECT_DIR": str(project),
        "PIPELINE_DIR": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "PATH": f"{shims}{os.pathsep}{env.get('PATH', os.environ.get('PATH', ''))}",
        "SPAWN_LOG": str(shim_log),
    }
    if recovery_enabled is not None:
        env2["REMEMBER_RECOVERY_ENABLED"] = recovery_enabled
    else:
        env2.pop("REMEMBER_RECOVERY_ENABLED", None)
    result = subprocess.run(
        [BASH, CAPTURE_GAP_CHECK.as_posix(), _SESSION_ID],
        env=env2, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"capture-gap-check.sh exited {result.returncode}: {result.stderr[-2000:]}"
    return spawns(shim_log)


def _merge_spawns(recorded: list[str]) -> list[str]:
    """The subset of COUNTED spawns that lib-memory-dir.sh's three-layer
    merge (and log.sh's own #668 flatten fallback) is known to cost: mktemp
    (the merged-config tmpfile, #362) and jq (the merge itself and the
    flatten). `cp`/`mv` are the merge's own file moves. Anything else
    (session_was_saved's own jq call, etc.) is a real, UNRELATED cost this
    test must not conflate with the merge.
    """
    return [s for s in recorded if s.split(" ", 1)[0] in ("mktemp", "jq")]


def _env_cache_diag(project: Path, home: Path, systmp: Path, env: dict) -> str:
    """Diagnostics only (never shipped): TMPDIR/TMP/TEMP/SYS_TMPDIR as the hook
    would see them, directory listings of systmp and .remember/tmp, and the
    cache key/path each side of lib-env-cache.sh computes for this exact
    project -- sourced directly (no hook side effects) so a mismatch between
    the publish-side key (post-resolve-paths.sh CLAUDE_PROJECT_DIR) and the
    load-side key (pre-resolve-paths.sh / REMEMBER_HOOK_CWD fallback) is
    visible without re-running the whole chain under `bash -x`.
    """
    parts = []
    for var in ("TMPDIR", "TMP", "TEMP"):
        parts.append(f"{var}={env.get(var)!r}")
    parts.append(f"computed SYS_TMPDIR (TMPDIR-or-/tmp)={(env.get('TMPDIR') or '/tmp')!r}")
    try:
        parts.append(f"systmp listing: {sorted(p.name for p in systmp.glob('*'))}")
    except OSError as exc:
        parts.append(f"systmp listing: could not list ({exc})")
    remember_tmp = project / ".remember" / "tmp"
    try:
        parts.append(
            f".remember/tmp listing: "
            f"{sorted(p.name for p in remember_tmp.glob('*')) if remember_tmp.exists() else 'DIR MISSING'}"
        )
    except OSError as exc:
        parts.append(f".remember/tmp listing: could not list ({exc})")

    lib = (REPO_ROOT / "scripts" / "lib-env-cache.sh").as_posix()
    probe = (
        f'source "{lib}"\n'
        '_remember_env_cache_path\n'
        'printf "KEY_PROJECT_DIR=%s\\nKEY_FILE=%s\\n" '
        '"$_REMEMBER_ENV_CACHE_PROJECT_DIR" "$_REMEMBER_ENV_CACHE_FILE"\n'
    )
    for label, overrides in (
        ("publish-side (CLAUDE_PROJECT_DIR set, post-resolve-paths.sh shape)",
         {"CLAUDE_PROJECT_DIR": str(project), "REMEMBER_HOOK_CWD": ""}),
        ("load-side (CLAUDE_PROJECT_DIR unset, REMEMBER_HOOK_CWD fallback, #469 shape)",
         {"CLAUDE_PROJECT_DIR": "", "REMEMBER_HOOK_CWD": str(project)}),
    ):
        probe_env = {**env, **overrides}
        try:
            result = subprocess.run(
                [BASH, "-c", probe], env=probe_env,
                capture_output=True, text=True, timeout=30, check=False,
            )
            parts.append(
                f"{label} key probe: rc={result.returncode} "
                f"stdout={result.stdout.strip()!r} stderr={result.stderr[-300:]!r}"
            )
        except (OSError, subprocess.SubprocessError) as exc:
            parts.append(f"{label} key probe: could not run ({exc})")

    # bash -x re-run of the warm path, trimmed to the env-cache publish/load
    # section -- the log is large, so only lines naming env_cache are kept.
    try:
        xtrace = subprocess.run(
            [BASH, "-x", str(SESSION_START)],
            env={
                **env,
                "CLAUDE_PROJECT_DIR": str(project),
                "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
                "HOME": str(home),
                "TMPDIR": str(systmp),
                "REMEMBER_DEFER": "0",
            },
            input=json.dumps({
                "session_id": _SESSION_ID,
                "transcript_path": "/does/not/matter/1079.jsonl",
                "hook_event_name": "SessionStart",
                "cwd": str(project),
                "source": "startup",
            }).encode("utf-8"),
            capture_output=True, timeout=60, check=False,
        )
        # This is diagnostics-only and must never itself crash the test it is
        # explaining: `text=True` decodes with the strict error handler, and a
        # `bash -x` trace of a WHOLE session-start-hook.sh run can carry a
        # byte sequence from this host's own locale/filesystem that is not
        # valid UTF-8 (observed on macos-latest CI, consistently, at the same
        # byte offset across four Python versions -- a real non-UTF-8 byte in
        # the trace, not a flake). `decode_bash_output` is the same tolerant
        # decode this repo's own bash-subprocess tests already use for
        # exactly this reason.
        all_lines = decode_bash_output(xtrace.stderr).splitlines()
        trace_lines = [l for l in all_lines if "env_cache" in l]
        parts.append(
            f"bash -x re-run of the warm path (env_cache lines, "
            f"{len(trace_lines)} of {len(all_lines)} total; rc={xtrace.returncode}):\n"
            + "\n".join(trace_lines[:120])
        )
        # Temporary, diagnostics-only widening (#1086 step 2): when the
        # env_cache-filtered view is empty but the full trace is short (the
        # observed windows-latest shape -- 31 total lines, 0 naming
        # env_cache), the hook took an early silent exit well before
        # publish; print the whole trace so that exit is visible instead of
        # an empty env_cache-filtered list.
        if not trace_lines and len(all_lines) < 200:
            parts.append(
                "FULL bash -x trace (env_cache-filtered view was empty):\n"
                + "\n".join(all_lines)
            )
    except (OSError, subprocess.SubprocessError) as exc:
        parts.append(f"bash -x re-run: could not run ({exc})")

    return "\n".join(parts)


@pytest.mark.parametrize("with_user_config", [False, True], ids=["no-config", "with-config"])
def test_fast_path_with_warm_cache_never_reruns_the_merge(tmp_path, with_user_config):
    """The actual #1079 claim: with the env cache warm (the ordinary case,
    since session-start-hook.sh always publishes it moments before
    dispatching this file), the fast path never re-sources log.sh's merge --
    zero jq/mktemp spawns beyond the ones `session_was_saved` etc. already
    cost on an UNRELATED path, with AND without a user config present (the
    "6 spawns" case the issue itself measured is the with-config
    parametrization).
    """
    project, remember, home = _project_and_home(tmp_path, with_user_config=with_user_config)
    systmp = tmp_path / "systmp"
    env = _warm_the_cache(project, home, systmp)

    # The real #1079 contract (post-CI-fix): session-start-hook.sh exports
    # REMEMBER_RECOVERY_ENABLED before it forks this script, so the fast
    # path never touches the cache FILE at all on the ordinary path -- see
    # _run_capture_gap_check_shimmed's own docstring for why this harness
    # sets it directly rather than exercising real process inheritance.
    # config_into's default is the bare word `true`; with_user_config writes
    # features.recovery=false into config.json, so the resolved value flips.
    recovery_enabled = "false" if with_user_config else "true"
    recorded = _run_capture_gap_check_shimmed(
        project, remember, env, tmp_path, recovery_enabled=recovery_enabled,
    )
    merge_spawns = _merge_spawns(recorded)

    assert not merge_spawns, (
        f"capture-gap-check.sh's fast path re-ran the config merge with a "
        f"warm env cache (with_user_config={with_user_config}): {merge_spawns}\n"
        f"full spawn log: {recorded}\n"
        f"{_env_cache_diag(project, home, systmp, env)}"
    )


def test_fast_path_falls_back_to_the_full_merge_when_both_caches_are_missing(tmp_path):
    """Positive control, paired with the test above: delete BOTH caches that
    sit on this path -- #1079's own env cache (`remember-env-*`) AND log.sh's
    pre-existing #668 flattened-config cache (`remember-config-cache-v2-*`,
    which the warm run below also primes as a side effect of its own normal
    operation and which alone would make a second `source log.sh` look cheap
    regardless of #1079) -- and prove the harness CAN see jq/mktemp spawns at
    all. Otherwise the "none found" assertion above could be passing because
    neither cache was ever really exercised, not because the fast path is
    actually skipping the merge.
    """
    project, remember, home = _project_and_home(tmp_path, with_user_config=True)
    systmp = tmp_path / "systmp"
    env = _warm_the_cache(project, home, systmp)

    cache_files = (
        list(systmp.glob("remember-env-*"))
        + list(systmp.glob("remember-config-cache-v2-*"))
    )
    assert cache_files, (
        "precondition: the warm run did not publish any cache file at all\n"
        f"{_env_cache_diag(project, home, systmp, env)}"
    )
    for f in cache_files:
        f.unlink()

    recorded = _run_capture_gap_check_shimmed(project, remember, env, tmp_path)
    merge_spawns = _merge_spawns(recorded)

    assert merge_spawns, (
        "positive control: with both caches deleted, capture-gap-check.sh's "
        "fast path should fall back to the full config merge, and the "
        f"harness saw no jq/mktemp spawns at all -- it cannot see this branch\n"
        f"full spawn log: {recorded}"
    )


def test_warm_cache_run_leaves_no_new_merged_config_tmpfile_in_shared_tmp(tmp_path):
    """#1079 point 2 (the #362-style leak): with a warm cache, the fast path
    never sources log.sh at all, so lib-memory-dir.sh's own
    `mktemp "${SYS_TMPDIR}/remember-config-XXXXXX"` never runs and nothing
    NEW named remember-config-* can appear in shared tmp after the child
    exits. session-start-hook.sh's own warm run legitimately leaves a
    PERSISTENT flattened-config cache here too (`remember-config-cache-v2-*`,
    unrelated to #1079) -- present BEFORE capture-gap-check.sh ever runs, so
    the claim is "no NEW file appears", not "nothing named remember-config-*
    exists at all".
    """
    project, remember, home = _project_and_home(tmp_path, with_user_config=True)
    systmp = tmp_path / "systmp"
    env = _warm_the_cache(project, home, systmp)

    # See test_fast_path_with_warm_cache_never_reruns_the_merge: the real
    # #1079 contract hands this scalar off via the environment, not the
    # cache file -- with_user_config=True resolves features.recovery=false.
    before = {p.name for p in systmp.glob("remember-config-*")}
    _run_capture_gap_check_shimmed(
        project, remember, env, tmp_path, recovery_enabled="false",
    )
    after = {p.name for p in systmp.glob("remember-config-*")}

    new_files = after - before
    assert not new_files, (
        f"capture-gap-check.sh's fast path left a NEW merged-config tmpfile "
        f"behind in shared tmp: {new_files}\n"
        f"{_env_cache_diag(project, home, systmp, env)}"
    )


def test_cache_miss_path_leaves_no_new_merged_config_tmpfile_in_shared_tmp(tmp_path):
    """#1086 review (item 1): the SLOW/cache-miss path -- where
    capture-gap-check.sh's own `trap _remember_cleanup_deferred_pid_file
    EXIT` (added for #1060's PID-marker cleanup) sits in the SAME process
    log.sh's config merge already installed its OWN EXIT trap in, to remove
    the merged-config tmpfile (lib-memory-dir.sh:704-725) -- is exactly
    where a bare (non-appending) `trap ... EXIT` silently replaces that
    prior trap and leaks the merge's own tmpfile. The warm-path sibling
    above never walks this branch at all (no caches ever get deleted
    there), so it cannot catch this specific leak -- this test deletes both
    caches first, forcing the full merge AND the deferred-PID trap install
    into the same process.
    """
    project, remember, home = _project_and_home(tmp_path, with_user_config=True)
    systmp = tmp_path / "systmp"
    env = _warm_the_cache(project, home, systmp)

    cache_files = (
        list(systmp.glob("remember-env-*"))
        + list(systmp.glob("remember-config-cache-v2-*"))
    )
    assert cache_files, (
        "precondition: the warm run did not publish any cache file at all\n"
        f"{_env_cache_diag(project, home, systmp, env)}"
    )
    for f in cache_files:
        f.unlink()

    # Exclude remember-config-cache-v2-* -- unlike the warm-path sibling
    # above, THIS test deletes it first, so a legitimate republish of it by
    # the fresh merge this test forces is expected and is not the leak
    # under test. The bare `remember-config-XXXXXX` mktemp name (no
    # `-cache-v2-` segment) is the one lib-memory-dir.sh's own EXIT trap
    # must remove.
    before = {p.name for p in systmp.glob("remember-config-*") if "-cache-v2-" not in p.name}
    _run_capture_gap_check_shimmed(project, remember, env, tmp_path)
    after = {p.name for p in systmp.glob("remember-config-*") if "-cache-v2-" not in p.name}

    new_files = after - before
    assert not new_files, (
        f"capture-gap-check.sh's SLOW/cache-miss path left a NEW merged-"
        f"config tmpfile behind in shared tmp -- its own "
        f"_remember_cleanup_deferred_pid_file EXIT trap replaced log.sh's "
        f"merge-cleanup trap instead of appending to it: {new_files}\n"
        f"{_env_cache_diag(project, home, systmp, env)}"
    )
