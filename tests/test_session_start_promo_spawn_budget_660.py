"""SessionStart's promo selection must not fork one `jq` per field, per entry,
per candidate (#660).

#660 reports `session-start-hook.sh` averaging ~12.5s per session start on
Windows/Git Bash across 36 real starts, against 0.5-1.3s for comparable
SessionStart hooks in the same sessions -- and names Windows process-spawn
cost (~50-200ms per subprocess/`jq` invocation under Git Bash, the reporter's
own measurement) as the plausible multiplier for a hook that shells out
repeatedly on the hot (foreground) path.

Walking that path (not assuming the reporter's own hypothesis is the right
lever, per the brief): `config()` was already collapsed to one `jq` process
per session by #232, and the recovery/consolidation forks below are already
backgrounded by #646 -- so they were not the lever either. What remained was
`_remember_compute_promo()`: one `jq -r '.promos | length'`, then FOUR more
`jq` calls per candidate entry (id/text/url/installed_key), and then, for
every candidate that survived those, the exact same `.plugins[$k]` query run
TWICE -- once to capture its value, once again just to inspect its exit
status. Measured on this file before #660 (`spawn_counting`'s PATH shim, one
promos.json candidate resolving on the first entry): 10 `jq` forks for the
promo mechanism alone, inside a hook that otherwise costs 1 (config()'s
one-pass load) + 1 (the final systemMessage JSON). After #660 folds the field
reads into one `@tsv` dump and the installed-plugins probe into one call:
4 total.

**Observed, not reasoned**: this suite runs on the CI/dev host's own `jq` and
counts *executions*, not wall-clock -- the same choice `tests/spawn_counting.py`
made for the post-tool-hook and prompt-hook budgets, because wall clock is
what differs between platforms and spawn count is what causes it. Whether the
per-spawn COST is 50-200ms is Git-Bash-under-Windows-specific and not
something this host can measure; only the count survives the trip -- what
this test pins is the number of forks, and the reasoned (not observed) claim
is that removing 6 of them removes 6 x that unmeasured per-spawn cost,
wherever it runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug
from tests.spawn_counting import make_shim_dir, spawns

SESSION = "eeeeeeee-0000-4000-8000-000000000660"

# Measured post-#660 (this file, one not-installed candidate resolving on the
# first promos.json entry): 4 `jq` forks. Pre-#660 the same scenario cost 10.
# The budget is the measured number, not a rounder one, so the next `jq` call
# added to this path trips it as a regression rather than hiding in slack --
# the same discipline test_post_tool_hook_spawns.py documents for its own
# budget.
PROMO_JQ_SPAWN_BUDGET = 4


def _run_with_shim(tmp_path: Path, extra_setup=None) -> tuple[list[str], subprocess.CompletedProcess]:
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    if extra_setup is not None:
        extra_setup(remember)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": "2", "plugins": {}}), encoding="utf-8"
    )

    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "SPAWN_LOG": str(log),
        "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
    }
    payload = json.dumps(
        {
            "session_id": SESSION,
            "transcript_path": f"/does/not/matter/{SESSION}.jsonl",
            "hook_event_name": "SessionStart",
            "cwd": "/does/not/matter",
        }
    )
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return spawns(log), result


def test_promo_selection_stays_within_the_jq_spawn_budget(tmp_path):
    """Positive control built in: the promo genuinely fires (a not-installed
    candidate exists), so this counts the SAME run the promo feature is
    supposed to speak on -- a budget measured against a run that emits
    nothing would not be measuring the path #660 is actually about.
    """
    lines, result = _run_with_shim(tmp_path)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed.get("systemMessage"), (
        "positive control failed: no promo fired, so a spawn count of 0 here "
        "would prove nothing about the path this budget is pinning"
    )

    jq_spawns = [line for line in lines if line.startswith("jq ")]
    assert len(jq_spawns) <= PROMO_JQ_SPAWN_BUDGET, (
        f"promo selection spawned {len(jq_spawns)} jq processes "
        f"(budget {PROMO_JQ_SPAWN_BUDGET}):\n" + "\n".join(jq_spawns)
    )


def _run_with_shim_no_jq(tmp_path: Path, extra_setup=None) -> tuple[list[str], subprocess.CompletedProcess]:
    """Same as `_run_with_shim`, except `jq` is unresolvable on PATH -- the
    shim for it is removed and the real system PATH is never appended, only
    the shim directory itself (every other COUNTED command still resolves,
    because each shim already execs the real absolute binary it found at
    creation time -- see spawn_counting.make_shim_dir). This forces every
    caller that reads $JQ onto the Python fallback, which is exactly the
    path #662's laziness fix must still resolve correctly.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": "2", "plugins": {}}), encoding="utf-8"
    )
    if extra_setup is not None:
        extra_setup(remember)

    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    jq_shim = shims / "jq"
    if jq_shim.exists():
        jq_shim.unlink()
    # Shims only intercept COUNTED commands -- anything else the hook chain
    # reaches (`mktemp`, `bash` itself for the outer exec, ...) still needs
    # to resolve to something real. A filtered copy of the real PATH minus
    # `jq` (same construction tests/test_jq_free_config.py already uses),
    # appended AFTER the shims dir, covers every such command while jq
    # stays genuinely unresolvable throughout.
    no_jq_bin = tmp_path / "no-jq-bin"
    no_jq_bin.mkdir()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name == "jq":
                continue
            target = no_jq_bin / name
            if target.exists() or target.is_symlink():
                continue
            try:
                os.symlink(os.path.join(d, name), target)
            except OSError:
                pass

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "SPAWN_LOG": str(log),
        "PATH": f"{shims}{os.pathsep}{no_jq_bin}",
    }
    payload = json.dumps(
        {
            "session_id": SESSION,
            "transcript_path": f"/does/not/matter/{SESSION}.jsonl",
            "hook_event_name": "SessionStart",
            "cwd": "/does/not/matter",
        }
    )
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return spawns(log), result


def test_python_detection_is_lazy_when_jq_is_present(tmp_path):
    """#662: session-start-hook.sh's foreground path must not fork a single
    python3/python/py candidate when jq is on PATH -- PYTHON is only read by
    four jq-LESS fallback call sites, none of which this run reaches.

    Negative control only would pass on a broken harness that spawns
    nothing at all; paired below (test_python_detection_still_resolves_when_
    jq_is_absent) with the positive control that the SAME mechanism genuinely
    probes for an interpreter once something jq-less actually needs it.
    """
    lines, result = _run_with_shim(tmp_path)
    assert result.returncode == 0, result.stderr

    python_spawns = [
        line for line in lines
        if line.startswith(("python3 ", "python "))
    ]
    assert python_spawns == [], (
        "jq is present, so nothing on this path should ever need an "
        "interpreter -- but the probe ran anyway:\n" + "\n".join(python_spawns)
    )


def test_python_detection_still_resolves_when_jq_is_absent(tmp_path):
    """Positive control for the test above: with jq genuinely unresolvable,
    the SAME lazy resolver must still find a working interpreter and the
    jq-less config paths must still produce a correct, non-empty config --
    laziness must never degrade into "broken when jq is absent" (the
    brief's own named trap for this issue).
    """

    def _seed_config(remember: Path) -> None:
        (remember / "config.json").write_text(
            json.dumps({"thresholds": {"memory_inject_max_bytes": 12345}}),
            encoding="utf-8",
        )

    lines, result = _run_with_shim_no_jq(tmp_path, extra_setup=_seed_config)
    assert result.returncode == 0, result.stderr

    python_spawns = [
        line for line in lines
        if line.startswith(("python3 ", "python "))
    ]
    assert python_spawns, (
        "jq is absent and this run's own config.json carries a real "
        "override -- some jq-less fallback call site must have reached "
        "for an interpreter, or the positive control proves nothing"
    )


def test_memory_injection_spawn_budget(tmp_path):
    """#664: rendering the MEMORY section for N present files must not fork
    one `wc` + one `tr` + one `basename` PER file -- batched into a single
    `wc -c` call across every present file, plus one `cat` per file (content
    still has to be read, so `cat` is not reducible below one-per-file).

    Six memory files (matching lib-memory-context.sh's own MEMORY_FILES
    list) gives the same shape as the real foreground path's worst case.
    """
    remember = tmp_path / "remember"
    remember.mkdir()
    (remember / "tmp").mkdir()
    memory_files = {
        "identity.md": "I am the user's memory.\n",
        "core-memories.md": "Core fact one.\n",
        "now.md": "Right now: testing.\n",
        "recent.md": "Recently: wrote a test.\n",
        "archive.md": "Long ago: archived.\n",
    }
    for name, body in memory_files.items():
        (remember / name).write_text(body, encoding="utf-8")

    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    env = {
        **os.environ,
        "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
        "SPAWN_LOG": str(log),
        "REMEMBER_DIR": str(remember),
        "PROJECT_DIR": str(tmp_path / "project"),
        "PLUGIN_ROOT": str(REPO_ROOT),
        "TODAY": "2026-09-12",
    }
    # Stubbed, not sourced from log.sh: log.sh pulls in lib-memory-dir.sh,
    # which unconditionally RE-RESOLVES REMEMBER_DIR from PROJECT_DIR (the
    # legacy "${{PROJECT_DIR}}/.remember" default) -- overwriting the exact
    # REMEMBER_DIR this test just populated. The render only ever calls
    # config() for one threshold, so a stub is both simpler and avoids that
    # whole unrelated resolution chain.
    # #665 (part of #660): lib-memory-context.sh now calls config_into and
    # _remember_forward_slash_into (printf -v, no command-substitution
    # subshell) instead of capturing $(config ...) / $(_remember_forward_slash
    # ...) -- both stubs need their `_into` sibling too.
    script = f"""
    set -e
    config() {{ printf '%s' "$2"; }}
    config_into() {{ printf -v "$1" '%s' "$3"; }}
    _remember_forward_slash() {{ printf '%s' "$1"; }}
    _remember_forward_slash_into() {{ printf -v "$1" '%s' "$2"; }}
    source "{REPO_ROOT}/scripts/lib-clock.sh"
    source "{REPO_ROOT}/scripts/lib-memory-context.sh"
    _remember_memory_paths
    _remember_render_memory_section
    """
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr

    # Positive control: every file's content actually made it into the
    # render -- a budget of 0 spawns on a render that printed nothing would
    # prove nothing about the path #664 is about.
    for body in memory_files.values():
        assert body.strip() in result.stdout, (
            f"memory content missing from render -- {result.stdout!r}"
        )

    wc_spawns = [line for line in spawns(log) if line.startswith("wc ")]
    basename_spawns = [line for line in spawns(log) if line.startswith("basename ")]
    tr_spawns = [line for line in spawns(log) if line.startswith("tr ")]
    assert basename_spawns == [], (
        f"basename should never fork -- ${{MFILE##*/}} replaces it: {basename_spawns}"
    )
    assert tr_spawns == [], (
        f"tr -d ' ' should never fork -- `read` already trims: {tr_spawns}"
    )
    # One batched `wc -c` over the five present files, not five separate ones.
    assert len(wc_spawns) <= 1, (
        f"expected at most 1 batched wc call for 5 files, got "
        f"{len(wc_spawns)}:\n" + "\n".join(wc_spawns)
    )


CAPTURE_SEEN_KEEP = 200  # mirrors session-start-hook.sh's own constant


def _seed_capture_seen(remember: Path, count: int) -> Path:
    seen_dir = remember / "tmp" / "capture-alive.d"
    seen_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (seen_dir / f"session-{i:04d}").write_text("x", encoding="utf-8")
    return seen_dir


def _prune_spawns(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("ls ") and "capture-alive.d" in line]


def test_capture_seen_prune_is_gated_on_the_threshold(tmp_path):
    """#666: the capture-alive.d prune must not fork `ls -t | tail` on every
    single session start when the directory exists but is nowhere near
    CAPTURE_SEEN_KEEP (200) entries. The pre-#666 code forked on a bare
    `[ -d ]`, so a directory that EXISTS is the case that separates the
    two -- an absent directory never forked under either (the first draft
    of this test only ran that shape, and would have passed against the
    old code: Explore self-review finding on #675).
    """
    lines, result = _run_with_shim(
        tmp_path, extra_setup=lambda remember: _seed_capture_seen(remember, 2)
    )
    assert result.returncode == 0, result.stderr
    seen_dir = tmp_path / "project" / ".remember" / "tmp" / "capture-alive.d"
    assert len(list(seen_dir.iterdir())) == 2, "the run must not touch an under-threshold store"
    assert _prune_spawns(lines) == [], (
        "capture-alive.d exists with 2 entries, far under the keep threshold, "
        f"so the prune must not fork `ls` against it: {_prune_spawns(lines)}"
    )


def test_capture_seen_prune_still_fires_over_the_threshold(tmp_path):
    """Positive control for the gate above: one entry over CAPTURE_SEEN_KEEP,
    the prune must run -- `ls -t` observed on the spawn log -- and leave
    exactly CAPTURE_SEEN_KEEP entries behind. A gate that skipped the
    prune outright would pass the test above and fail here.
    """
    lines, result = _run_with_shim(
        tmp_path, extra_setup=lambda remember: _seed_capture_seen(remember, CAPTURE_SEEN_KEEP + 1)
    )
    assert result.returncode == 0, result.stderr
    assert _prune_spawns(lines), (
        f"{CAPTURE_SEEN_KEEP + 1} entries is over the threshold, so the prune must "
        "actually run -- no `ls` against capture-alive.d was observed"
    )
    seen_dir = tmp_path / "project" / ".remember" / "tmp" / "capture-alive.d"
    remaining = len(list(seen_dir.iterdir()))
    assert remaining == CAPTURE_SEEN_KEEP, (
        f"prune left {remaining} entries; expected exactly {CAPTURE_SEEN_KEEP}"
    )


def test_memory_render_has_no_bash_3_2_associative_arrays(tmp_path):
    """Self-review finding (#662/#664): `declare -A` is silently ACCEPTED by
    bash 3.2 (it just creates a plain indexed array instead of erroring),
    so the bug does not show up until the very next `${arr[$key]}` lookup,
    which throws a fatal "syntax error: operand expected" there instead --
    aborting the MEMORY render with nothing injected and nothing visible
    beyond stderr, on the one platform (stock macOS `/bin/bash`) this
    repo's own lib-clock.sh and lib-lock.sh document as a supported floor
    specifically BECAUSE it lacks associative arrays.

    Two checks: a static grep that fires on *any* shell script (catches a
    regression anywhere, on any host, with no real bash 3.2 needed), and a
    functional run against the real `/bin/bash` on this machine when it
    actually identifies as bash < 4 -- the same repro that caught this bug.
    """
    import subprocess as _sp

    # Two-stage filter, not a single "exclude comments" regex: the first
    # attempt here (`^\s*[^#\s].*declare -A`) silently failed to fire on a
    # line where `declare -A` is the FIRST thing on it, because the
    # character class consumes the 'd' of "declare", leaving no second
    # "declare -A" substring behind for `.*declare -A` to find afterwards
    # -- caught only by deliberately reintroducing the bug during self-
    # review and finding this check stayed green. `grep -n` first, then a
    # plain Python check that the content after "path:lineno:" does not
    # start with `#` (this very comment block names the flag in prose, so
    # that line must stay excluded).
    hits = _sp.run(
        ["grep", "-rn", "declare -A", str(REPO_ROOT / "scripts")],
        capture_output=True, text=True, check=False,
    )
    real_hits = [
        line for line in hits.stdout.splitlines()
        if not re.match(r"^[^:]*:[0-9]+:\s*#", line)
    ]
    assert not real_hits, (
        "declare -A reintroduced -- bash 3.2 accepts it silently and only "
        "fails on the next lookup, far from this line:\n" + "\n".join(real_hits)
    )

    version_check = subprocess.run(
        ["/bin/bash", "-c", "echo ${BASH_VERSINFO[0]}"],
        capture_output=True, text=True, check=False,
    )
    if version_check.returncode != 0 or not version_check.stdout.strip().isdigit():
        pytest.skip("no usable /bin/bash on this host to check the real floor")
    if int(version_check.stdout.strip()) >= 4:
        pytest.skip("this host's /bin/bash is not the bash-3.2-floor platform")

    remember = tmp_path / "remember"
    remember.mkdir()
    (remember / "identity.md").write_text("memory content\n", encoding="utf-8")
    script = f"""
    config() {{ printf '%s' "$2"; }}
    _remember_forward_slash() {{ printf '%s' "$1"; }}
    source "{REPO_ROOT}/scripts/lib-clock.sh"
    source "{REPO_ROOT}/scripts/lib-memory-context.sh"
    _remember_memory_paths
    _remember_render_memory_section
    """
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={**os.environ, "REMEMBER_DIR": str(remember),
             "PROJECT_DIR": str(tmp_path / "project"),
             "PLUGIN_ROOT": str(REPO_ROOT), "TODAY": "2026-09-12"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, (
        f"real bash 3.2 floor: render aborted -- stderr={result.stderr!r}"
    )
    assert "memory content" in result.stdout, (
        f"positive control failed: file content missing from render -- "
        f"{result.stdout!r}"
    )


def _floor_bash_or_skip() -> str:
    """The real `/bin/bash` when it is the bash < 4.4 floor, else skip.

    `"${arr[@]}"` over an EMPTY array is an "unbound variable" error under
    `set -u` on every bash before 4.4 and silently fine on 4.4+, so the
    class of bug pinned below can only be demonstrated on a floor bash --
    stock macOS's 3.2. On hosts without one this is a skip with the reason
    stated, never a pass: the CI macOS legs are where it actually runs.
    """
    version_check = subprocess.run(
        ["/bin/bash", "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
        capture_output=True, text=True, check=False,
    )
    ver = version_check.stdout.strip()
    if version_check.returncode != 0 or not re.match(r"^\d+\.\d+$", ver):
        pytest.skip("no usable /bin/bash on this host to check the real floor")
    major, minor = (int(x) for x in ver.split("."))
    if (major, minor) >= (4, 4):
        pytest.skip(
            f"this host's /bin/bash is {ver}; the empty-array-under-set-u "
            "error only exists below 4.4"
        )
    return "/bin/bash"


def _render_under_set_u(bash: str, remember: Path, project: Path, source: str) -> subprocess.CompletedProcess:
    script = f"""
    set -u
    config() {{ printf '%s' "$2"; }}
    _remember_forward_slash() {{ printf '%s' "$1"; }}
    source "{REPO_ROOT}/scripts/lib-clock.sh"
    source "{REPO_ROOT}/scripts/lib-memory-context.sh"
    _remember_memory_paths
    _remember_render_memory_section
    """
    env = {**os.environ, "REMEMBER_DIR": str(remember), "PROJECT_DIR": str(project),
           "PLUGIN_ROOT": str(REPO_ROOT), "TODAY": "2026-09-12"}
    if source:
        env["SESSION_START_SOURCE"] = source
    else:
        env.pop("SESSION_START_SOURCE", None)
    return subprocess.run([bash, "-c", script], env=env,
                          capture_output=True, text=True, timeout=30, check=False)


def test_compact_render_with_no_identity_file_survives_set_u_on_the_bash_floor(tmp_path):
    """CI's macOS legs on #675 (bash 3.2): at SESSION_START_SOURCE=compact
    every non-identity file is skipped, so with no identity file the
    batched-`wc` present list is EMPTY -- and `for MFILE in "${arr[@]}"`
    over an empty array is a fatal "unbound variable" under `set -u` on
    bash < 4.4, which save-session.sh / run-consolidation.sh's publish path
    runs under. Nothing was injected and rc was 127 with an empty stderr
    (the render's own stderr is discarded by its caller). bash 5 -- the
    developer lane's and the Linux legs' bash -- hides it entirely, which
    is why it reached CI. Every iteration over an array that can be empty
    is now count-guarded; this pins the exact shape that failed.
    """
    bash = _floor_bash_or_skip()
    remember = tmp_path / "remember"
    remember.mkdir()
    (remember / "core-memories.md").write_text("core only, no identity\n", encoding="utf-8")

    compact = _render_under_set_u(bash, remember, tmp_path / "project", "compact")
    assert compact.returncode == 0, (
        f"compact render with an empty present list aborted under set -u on "
        f"{bash}: rc={compact.returncode} stderr={compact.stderr!r}"
    )
    assert "unbound variable" not in compact.stderr
    # The deferred list still names the skipped file: the guard did not
    # silence the branch, it only stopped the empty-array expansion.
    assert "core-memories.md" in compact.stdout, compact.stdout

    # Positive control -- the same harness renders content on the same
    # bash when the present list is NOT empty, so a green above is not a
    # harness that ran nothing.
    full = _render_under_set_u(bash, remember, tmp_path / "project", "")
    assert full.returncode == 0, full.stderr
    assert "core only, no identity" in full.stdout, full.stdout
