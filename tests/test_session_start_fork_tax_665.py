"""#665 (part of #660): a `$( )` command substitution around a builtin still
forks a subshell to capture the output, even though nothing inside it execs
an external process. `tests/spawn_counting.py` counts execs, not forks -- a
`$(printf ...)` or `$(config ...)` on the flattened-cache-hit path shows up
there as zero spawns even though bash still pays a fork for the substitution
itself. This file counts THAT fork directly, with `bash -x` and a `PS4` that
carries `$BASHPID`: every real fork() gets bash a brand-new pid, so the
number of DISTINCT pids seen in a trace (other than the "home" pid the probe
started at) is the number of forks, regardless of how many statements ran
inside any one of them or whether two forks happened to sit at the same
nesting depth as siblings.

(`$BASH_SUBSHELL` -- the more obvious probe variable -- was tried first and
rejected: it reports NESTING DEPTH, not fork count, so N sibling `$(printf
...)` substitutions at the same depth, run back to back with no trace line
ever falling back to depth 0 between them, undercounts to "however many
depth increases occurred" rather than N. Measured directly against the pre-
#665 slug-sed builder below: the depth-transition count came back 2 for a
builder that pays 22 real forks. `$BASHPID` does not have this failure mode
because two sibling subshells are still two distinct child processes.)

Every "must not fork" assertion here is paired with a "must fork" positive
control using the SAME harness and (where possible) the SAME call shape,
still written as `$(...)` -- so a broken harness that sees no forks anywhere
cannot pass silently (the negative-assertion-needs-a-positive-control rule
this repo's CLAUDE.md states directly).

Two closely-read positive controls matter more than they look:

* `config()`'s own pre-existing `( LC_ALL=C; [[ ... ]] )` key-shape guard is
  ALREADY a subshell, deliberately (its own comment in log.sh explains why:
  scoping the locale override to one match). That fork is NOT what #665
  removes and this file does not claim otherwise -- `config_into()` still
  pays it. What #665 removes is the OUTER command-substitution fork
  `$(config ...)` adds on top of that -- so the pinned claim is "config_into
  forks exactly one FEWER time than `$(config ...)` does", not "config_into
  forks zero times".

* `log()`'s message control-byte scrub (`$(printf ... | LC_ALL=C tr ...)`)
  is deliberately NOT touched here. #621 tried twice to remove it (a bracket
  class, then a byte-range check) and both went red on live CI in ways this
  repo could not reproduce locally, so #621 was closed as not worth the
  fragility and log() forks this unconditionally, same as before #621. The
  timestamp fork (`$(_remember_date ...)`) is a different site and IS
  removed -- `log()`'s total fork count drops from 2 to 1, not to 0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS = REPO_ROOT / "scripts" / "resolve-paths.sh"
DETECT_TOOLS = REPO_ROOT / "scripts" / "detect-tools.sh"
BOOTSTRAP_DIRS = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
LIB_SLUG = REPO_ROOT / "scripts" / "lib-slug.sh"

BASH = resolve_bash()


def _bash_has_bashpid(bash: str | None) -> bool:
    """$BASHPID (bash >= 4.0) is what this whole file's fork counter relies
    on -- each real fork() gets a brand-new pid, unlike `$BASH_SUBSHELL`
    (nesting depth, undercounts sibling forks) or `$$` (bash's own docs: `$$`
    is NOT reset in a subshell, so every sibling would report the SAME
    "process"). Stock macOS ships bash 3.2 as `/bin/bash`
    (tests/test_lock_primitive.py's own `test_self_id_differs_between_sibling_subshells`
    documents the identical gap for lib-lock.sh's self-id, and lib-lock.sh's
    `_lock_self_set` falls back to `sh -c 'echo $PPID'` for exactly this
    reason) -- whether THIS host's `bash` (found by `resolve_bash()`, which
    trusts whatever is on PATH, not necessarily `/bin/bash`) is old enough to
    lack it is a real, host-dependent question, not a hypothetical one.
    Checked directly rather than assumed, so a host where `bash` happens to
    resolve to the pre-4.0 floor skips loudly instead of failing every test
    in this file on "harness produced no trace lines at all" -- a skip this
    file's own coverage note (below) names, not a silent pass.
    """
    if bash is None:
        return False
    result = subprocess.run(
        [bash, "-c", "echo ${BASHPID:-}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    return result.returncode == 0 and result.stdout.strip().isdigit()


_HAS_BASHPID = _bash_has_bashpid(BASH)
pytestmark = pytest.mark.skipif(
    BASH is None or not _HAS_BASHPID,
    reason=(
        "no usable bash found" if BASH is None else
        "this host's `bash` lacks $BASHPID (pre-4.0, e.g. stock macOS "
        "/bin/bash 3.2) -- this whole file's fork counter depends on it "
        "(see _bash_has_bashpid's docstring); untested on that floor rather "
        "than reporting a false pass or a spurious 'broken probe' failure"
    ),
)

START = "===FORKPROBE-START==="
END = "===FORKPROBE-END==="
PID_RE = re.compile(r"PID=(\d+)")


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    return home, project, remember


def _forks_from_trace(body: str) -> int:
    """Number of forks in a traced region: distinct $BASHPID values seen,
    minus one for the "home" pid the anchor (`:`, the first traced line)
    ran in. Each real fork() gets bash a brand-new pid, so this counts
    forks regardless of nesting depth or how many statements shared one."""
    pids = PID_RE.findall(body)
    assert pids, f"harness produced no trace lines at all -- broken probe: {body!r}"
    return len(set(pids)) - 1


def _count_forks(tmp_path: Path, home: Path, project: Path, probe: str) -> int:
    """Run `probe` after sourcing the real pipeline files in a cache-warm
    state, tracing ONLY the probe itself (plus one anchor command `:`
    immediately before it, whose pid is the "home" pid), and return how
    many forks the probe made.

    Sourcing the pipeline files themselves does its own forking (jq, stat,
    etc.) that has nothing to do with what the probe is being asked to
    prove, hence tracing only opens around the probe.
    """
    script = f"""
set -eu
REMEMBER_PATHS_SOFT_FAIL=1 source "{RESOLVE_PATHS.as_posix()}" || exit 99
PLUGIN_ROOT="$PIPELINE_DIR"
source "{DETECT_TOOLS.as_posix()}" || exit 99
source "{BOOTSTRAP_DIRS.as_posix()}" || exit 99
source "{LOG_SH.as_posix()}" 2>/dev/null
# Warm the flattened-cache table once, outside the traced region.
config '.warm.me' 'x' >/dev/null
exec 2>&1
echo "{START}"
PS4='+PID=${{BASHPID}} '
set -x
:
{probe}
set +x
echo "{END}"
"""
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_HOOK_CWD": str(project),
        "PATH": os.environ["PATH"],
    }
    result = subprocess.run(
        [BASH, "-c", script], env=env, capture_output=True, text=True,
        errors="replace", timeout=30, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    out = result.stdout
    assert START in out and END in out, f"probe markers missing: {out!r}"
    body = out.split(START, 1)[1].split(END, 1)[0]
    return _forks_from_trace(body)


def _warm_config(tmp_path: Path, home: Path, project: Path, remember: Path):
    cfg = remember / "config.json"
    cfg.write_text(json.dumps({"cooldowns": {"save_seconds": 42}}), encoding="utf-8")
    script = f"""
set -eu
REMEMBER_PATHS_SOFT_FAIL=1 source "{RESOLVE_PATHS.as_posix()}" || exit 99
PLUGIN_ROOT="$PIPELINE_DIR"
source "{DETECT_TOOLS.as_posix()}" || exit 99
source "{BOOTSTRAP_DIRS.as_posix()}" || exit 99
source "{LOG_SH.as_posix()}" 2>/dev/null
config '.cooldowns.save_seconds' '0'
"""
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_HOOK_CWD": str(project),
        "PATH": os.environ["PATH"],
    }
    result = subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    cache = remember / "tmp" / "config.rcfg"
    assert cache.is_file()
    now = time.time()
    os.utime(cache, (now + 5, now + 5))


# ---------------------------------------------------------------------------
# config_into: removes exactly the outer command-substitution fork on top of
# the pre-existing (deliberate) LC_ALL guard subshell -- not "zero forks".
# ---------------------------------------------------------------------------

def test_config_into_forks_only_the_preexisting_lc_all_guard(tmp_path):
    home, project, remember = _project(tmp_path)
    _warm_config(tmp_path, home, project, remember)
    n = _count_forks(
        tmp_path, home, project,
        'config_into RESULT ".cooldowns.save_seconds" "0"; echo "got=$RESULT"',
    )
    assert n == 1, (
        "config_into should fork exactly once -- the pre-existing "
        "( LC_ALL=C; [[ ... ]] ) key-shape guard, which #665 does not "
        f"touch -- got {n}"
    )


def test_command_substitution_around_config_forks_one_more_than_config_into(tmp_path):
    """Positive control, same harness, same cache-hit state, same call: the
    OLD `$(config ...)` idiom pays the guard fork PLUS its own outer
    command-substitution fork -- proving the probe can see the extra fork
    #665 removes, and that config_into's count above is not simply the
    harness reporting nothing."""
    home, project, remember = _project(tmp_path)
    _warm_config(tmp_path, home, project, remember)
    n_sub = _count_forks(
        tmp_path, home, project,
        'RESULT=$(config ".cooldowns.save_seconds" "0"); echo "got=$RESULT"',
    )
    n_into = _count_forks(
        tmp_path, home, project,
        'config_into RESULT ".cooldowns.save_seconds" "0"; echo "got=$RESULT"',
    )
    assert n_sub == 2, f"positive control failed -- expected 2 forks from $(config ...), got {n_sub}"
    assert n_sub == n_into + 1, (
        f"$(config ...) should fork exactly ONE more time than config_into "
        f"(the outer capture subshell) -- got n_sub={n_sub}, n_into={n_into}"
    )


# ---------------------------------------------------------------------------
# log(): the timestamp fork is removed (_remember_date_into), the message
# control-byte scrub fork is deliberately NOT (#621's history) -- net one
# fork remains, down from two.
# ---------------------------------------------------------------------------

def test_log_forks_once_for_the_deliberately_kept_message_scrub(tmp_path):
    home, project, remember = _project(tmp_path)
    _warm_config(tmp_path, home, project, remember)
    n = _count_forks(tmp_path, home, project, 'log "probe" "hello"')
    assert n == 2, (
        "log() should fork exactly twice now -- printf and tr, the two "
        "halves of the message control-byte scrub pipeline (kept "
        "deliberately, #621) -- the timestamp fork must be gone (it alone "
        f"was a third). got {n}"
    )


def test_command_substitution_around_remember_date_still_forks(tmp_path):
    home, project, remember = _project(tmp_path)
    _warm_config(tmp_path, home, project, remember)
    n = _count_forks(
        tmp_path, home, project,
        'RESULT=$(_remember_date +%H:%M:%S); echo "got=$RESULT"',
    )
    assert n == 1, f"positive control failed -- expected 1 fork for $(_remember_date ...), got {n}"


def test_remember_date_into_forks_nothing_on_the_builtin_path(tmp_path):
    """log() itself now calls this directly -- confirm it, in isolation,
    forks nothing on this host (bash >= 4.2, no REMEMBER_TZ), which is what
    makes the log() assertion above land on 1 instead of 2."""
    home, project, remember = _project(tmp_path)
    _warm_config(tmp_path, home, project, remember)
    n = _count_forks(tmp_path, home, project, '_remember_date_into RESULT +%H:%M:%S')
    assert n == 0, f"_remember_date_into must not fork on the builtin path, got {n}"


# ---------------------------------------------------------------------------
# _remember_build_slug_sed: ANSI-C ($'\\NNN') quoting instead of 22
# $(printf ...) forks, byte-identical output.
# ---------------------------------------------------------------------------

def _count_forks_bare(probe: str) -> int:
    script = f"""
set -eu
exec 2>&1
echo "{START}"
PS4='+PID=${{BASHPID}} '
set -x
:
{probe}
set +x
echo "{END}"
"""
    result = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True,
        errors="replace", timeout=30, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    out = result.stdout
    body = out.split(START, 1)[1].split(END, 1)[0]
    return _forks_from_trace(body)


def _slug_sed_program(tmp_path: Path) -> list[str]:
    script = f"""
set -eu
source "{LIB_SLUG.as_posix()}"
printf '%s\\0' "${{_REMEMBER_SLUG_SED[@]}}"
"""
    result = subprocess.run([BASH, "-c", script], capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    parts = result.stdout.split(b"\0")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return [p.decode("latin-1") for p in parts]


def test_slug_sed_program_forks_nothing_to_build():
    n = _count_forks_bare(f'source "{LIB_SLUG.as_posix()}"; _remember_build_slug_sed')
    assert n == 0, "_remember_build_slug_sed must not fork any subshell (ANSI-C quoting, not $(printf))"


_OLD_SLUG_BUILDER = r"""
_remember_build_slug_sed_OLD() {
    local cont
    cont="$(printf '\200')-$(printf '\277')"
    REFERENCE_SLUG_SED=(
        -e "s/$(printf '\360')[$(printf '\220')-$(printf '\277')][$cont][$cont]/--/g"
        -e "s/[$(printf '\361')-$(printf '\363')][$cont][$cont][$cont]/--/g"
        -e "s/$(printf '\364')[$(printf '\200')-$(printf '\217')][$cont][$cont]/--/g"
        -e "s/$(printf '\340')[$(printf '\240')-$(printf '\277')][$cont]/-/g"
        -e "s/[$(printf '\341')-$(printf '\354')][$cont][$cont]/-/g"
        -e "s/$(printf '\355')[$(printf '\200')-$(printf '\237')][$cont]/-/g"
        -e "s/[$(printf '\356')-$(printf '\357')][$cont][$cont]/-/g"
        -e "s/[$(printf '\302')-$(printf '\337')][$cont]/-/g"
        -e 's/[^a-zA-Z0-9]/-/g'
    )
}
"""


def test_old_printf_slug_builder_forks_22_times():
    """Positive control for the byte-compare test below: the OLD builder
    really does pay 22 subshell forks (one per `$(printf ...)`), so the
    harness proves it can see them before the new version claims zero."""
    n = _count_forks_bare(_OLD_SLUG_BUILDER + "\n_remember_build_slug_sed_OLD")
    assert n == 22, f"positive control failed -- expected 22 $(printf) forks, harness saw {n}"


def test_slug_sed_program_is_byte_identical_to_the_old_printf_builder(tmp_path):
    """Byte-compare the new ANSI-C-quoted builder against the pre-#665 one
    (the `$(printf ...)` shape, restored here verbatim as the reference),
    so the fork removal cannot silently change what gets matched."""
    script = f"""
set -eu
{_OLD_SLUG_BUILDER}
_remember_build_slug_sed_OLD
printf '%s\\0' "${{REFERENCE_SLUG_SED[@]}}"
"""
    result = subprocess.run([BASH, "-c", script], capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    parts = result.stdout.split(b"\0")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    reference = [p.decode("latin-1") for p in parts]

    actual = _slug_sed_program(tmp_path)
    assert actual == reference, "new sed program bytes differ from the old $(printf ...) builder"
