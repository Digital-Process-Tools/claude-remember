"""A cost pin for the per-tool-call path, closing the gap #330 named in the
substring guard #329 shipped (tests/test_case_divergence_298.py).

#299 needed the per-tool-call hook to be cheap; the byte-identity pin #329
loosened only checked whether the file NAMED three strings. #330: "a
subprocess spawn of anything that is not git" and "an extra file read, or a
loop" are not in that vocabulary at all, and neither is a wrapper that spawns
git under a different literal (`GIT=git; "$GIT" -C ...`, `command git`).

This file measures the property #299 actually cares about -- cost -- the way
tests/test_post_tool_fast_path_350.py already measures total spawn count on
the warm path, with two additions:

  * the git-wrapper vectors named in #330 are reproduced against a SCRATCH
    copy of the hook and shown to trip the existing spawn pin, so that pin's
    coverage is a measured fact rather than an assumption;
  * a NEW pin for the vector the spawn count literally cannot see: a bash
    builtin `read` that touches a file without forking anything. Counted via
    `bash -x` (xtrace), which needs no ptrace-level tooling and therefore
    works the same way on every platform this repo tests -- CI has never had
    strace/dtrace available, and this needs neither.

What this deliberately does NOT cover, because nothing here can see it: a
pure compute loop with no read, no write and no spawn adds real per-call cost
and is invisible to every pin in this file, including the new one. See
`test_a_pure_compute_loop_is_invisible_to_every_pin_here` -- it is not a gap
this file closes silently, it is a gap this file states.

A second, narrower gap in the read pin itself: `_is_read_builtin` matches
`read`, `mapfile` and `readarray` by name, because all three are traced as
their own xtrace line. `x=$(< file)` -- bash's own fast-path substitute for
`cat file` -- is not: it produces NO separate trace line at all (confirmed
locally: `bash -x` on a script containing it prints only the resulting
assignment, never a `read` or a `cat`), so it is invisible to xtrace itself,
not merely to this parser. Nothing in this file can catch that vector; it is
named here rather than left for a reader to discover by trying it.

Also: a single extra spawn or a single extra builtin read, on its own, may
fall inside a pin's own slack (test_post_tool_fast_path_350's is +2; the read
pin below carries the same). Slack exists so a legitimate one-spawn fix does
not turn every future PR red; it is also, by construction, the width of a
regression these pins cannot catch. Both budgets are measured, not guessed,
and both slacks are the same size and for the same reason as the existing
spawn budget.

A third gap, found on macOS CI rather than reasoned about (#330 follow-up):
the first version of this file wrote `config.json` with a plain
`write_text()` instead of `tests.env_cache.write_config()`, and never
verified with `tests.env_cache.EnvCacheProbe` that a "warm" run had actually
replayed the cache rather than re-resolved. `scripts/lib-env-cache.sh`
refuses its own cache unless it is `-nt` every config layer, and bash 3.2's
`-nt` (macOS system bash, confirmed via `env_cache.nt_granularity() ==
"second"` on this platform) compares whole seconds -- so a config written in
the same wall-clock second as the cache publish TIES rather than loses, the
cache is rejected, and every subsequent "warm" measurement in this file was
silently re-resolving the full chain instead. Reproduced deterministically
by forcing that tie by hand (`os.utime` matching the cache's own whole-second
mtime) before this fix: same spawn signature CI reported --
`python3 -V`, `git rev-parse`, the `jq -s` merge, the trap-stripping `sed`,
the temp-file sweep. Fixed the same way `test_post_tool_fast_path_350.py`
already does: `write_config()` backdates the config layer well past any
second boundary, and every measured (non-priming) run below is bracketed by
an `EnvCacheProbe` that fails loudly, naming which run went cold and why,
instead of silently measuring the wrong path. This is a harness bug, not a
platform limit -- every platform's bash refuses a same-second-or-older
cache, this file only failed to guarantee it never asked one to.
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

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.slug import session_dir_slug as _slug
from tests.env_cache import EnvCacheProbe, write_config
from tests.spawn_counting import make_shim_dir
from tests.spawn_counting import spawns as _spawn_lines

TRANSCRIPT_LINE = '{"type":"assistant","message":{"content":"x"}}\n'

# Restated from test_post_tool_fast_path_350.py rather than imported: this
# file mutates a SCRATCH COPY of post-tool-hook.sh per test, and importing
# that module's HOOK/PROMPT_HOOK constants (bound to the real REPO_ROOT
# tree) would invite a future edit here to accidentally run against them
# instead of the scratch copy. FAST_PATH_SPAWN_BUDGET is a pure number and
# safe to share; its derivation and slack are documented at the import site.
from tests.test_post_tool_fast_path_350 import FAST_PATH_SPAWN_BUDGET

# -- Scratch plugin: a full copy, so a mutation to post-tool-hook.sh cannot
#    touch the tree every other test in this repo runs against -----------

def _scratch_plugin(tmp_path: Path) -> Path:
    plugin = tmp_path / "plugin"
    subprocess.run(["cp", "-R", str(REPO_ROOT), str(plugin)], check=True,
                    capture_output=True)
    return plugin


def _patch(plugin: Path, old: str, new: str) -> None:
    """Mutate the SCRATCH copy of post-tool-hook.sh. Requires `old` to be
    present so a rename or reflow of the file upstream fails loudly here
    instead of silently patching nothing."""
    hook = plugin / "scripts" / "post-tool-hook.sh"
    body = hook.read_text(encoding="utf-8")
    assert old in body, f"anchor text not found in post-tool-hook.sh: {old!r}"
    hook.write_text(body.replace(old, new, 1), encoding="utf-8")


# -- Fixture project, independent of the plugin copy ----------------------

def _project(tmp_path: Path, *, jsonl_lines: int = 60, session_id: str = "sess-1"):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (session_dir / (session_id + ".jsonl")).write_text(
        TRANSCRIPT_LINE * jsonl_lines, encoding="utf-8",
    )
    # Through write_config, not a plain write_text: the cache this fixture
    # relies on being warm is refused unless it is `-nt` this file, and bash
    # 3.2's `-nt` (macOS system bash) compares whole SECONDS. A config
    # written in the same wall-clock second as the cache publish that
    # follows it TIES rather than loses, the cache is rejected, and every
    # "warm" measurement in this file silently re-resolves instead (#303,
    # reproduced concretely for this file as a macOS-CI-only red -- see the
    # module docstring). write_config backdates past any second boundary.
    write_config(remember / "config.json",
                 {"thresholds": {"delta_lines_trigger": 50}})
    (remember / "tmp" / "last-save-ts").write_text(str(int(time.time())),
                                                     encoding="utf-8")
    return home, project, remember


def _project_with_prior_save(tmp_path: Path, *, jsonl_lines: int = 60,
                              session_id: str = "sess-1", position: int = 30):
    """Like `_project`, but with a prior save already landed: last-save.json
    plus its #353 sidecar (`position.<session_id>`), agreeing with each
    other and bounded well inside `jsonl_lines`. `_project` on its own
    creates neither, so the warm run it feeds always takes LAST_LINE=0's
    fall-through and never walks the sidecar branch #353 added -- the gap
    #395 reports.

    Written directly in the schema `pipeline.shell save-position` itself
    produces (pipeline/shell.py:254-319: last-save.json is
    `{"sessions": {id: line}, "session": id, "line": line}`, the sidecar is
    the bare integer), rather than by shelling out to that command -- this
    fixture must not add a spawn of its own to a measurement that counts
    spawns.
    """
    home, project, remember = _project(tmp_path, jsonl_lines=jsonl_lines,
                                        session_id=session_id)
    last_save = remember / "tmp" / "last-save.json"
    last_save.write_text(
        json.dumps({"sessions": {session_id: position},
                    "session": session_id, "line": position}),
        encoding="utf-8",
    )
    sidecar = remember / "tmp" / ("position." + session_id)
    sidecar.write_text(str(position), encoding="utf-8")
    return home, project, remember


def _env(tmp_path: Path, home: Path, project: Path, plugin: Path) -> dict:
    tmpdir = tmp_path / "systmp"
    tmpdir.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "TMPDIR": str(tmpdir),
    }
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ",
                  "REMEMBER_NESTED_SUMMARIZER"):
        env.pop(stale, None)
    return env


def _prime(env: dict, plugin: Path) -> None:
    """One prompt-hook call publishes the resolution the post-tool hook's
    fast path then replays -- the same #350 pattern every sibling file in
    this repo uses, run against the SCRATCH plugin's own copy of the prompt
    hook so PIPELINE_DIR agrees with what the mutated post-tool hook will
    see."""
    result = subprocess.run(
        ["bash", str(plugin / "scripts" / "user-prompt-hook.sh")],
        env=env, input=b"", capture_output=True, timeout=120, check=False,
    )
    assert result.returncode == 0, "priming run failed: " + repr(result.stderr[:400])


def _reap(remember: Path) -> None:
    pid_file = remember / "tmp" / "save-session.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


# -- The new pin: bash builtin `read`, counted via xtrace -----------------

_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# `read` is the builtin every call site in this repo happens to use today,
# but it is not the only one that turns a redirected file into a variable
# with no subprocess: `mapfile`/`readarray` (bash >= 4) do the same thing in
# one traced command instead of a loop. A pin that only recognised the
# literal word `read` would call a rewrite from a `while read` loop to
# `mapfile -t arr < file` a cost reduction when it is exactly the same file
# being opened. Both names are checked for the same reason.
_READ_LIKE_BUILTINS = frozenset({"read", "mapfile", "readarray"})


def _is_read_builtin(cmd: str) -> bool:
    """`IFS= read -r x` traces as one command with the assignment prefixed --
    strip any number of `NAME=value` prefixes before asking whether the next
    word is exactly one of `_READ_LIKE_BUILTINS`. Exact-match, not substring:
    `read-position` is a positional argument to `$PYTHON -m pipeline.shell`,
    not the builtin, and a naive `\bread\b` regex would wrongly match it (a
    hyphen is a word boundary)."""
    tokens = cmd.split()
    i = 0
    while i < len(tokens) and _ASSIGN.match(tokens[i]):
        i += 1
    return i < len(tokens) and tokens[i] in _READ_LIKE_BUILTINS


def _read_builtin_lines(stderr: bytes) -> list[str]:
    text = stderr.decode("utf-8", "replace")
    out = []
    for line in text.splitlines():
        stripped = line.lstrip("+").strip()
        if _is_read_builtin(stripped):
            out.append(line)
    return out


# Measured on macOS bash 3.2.57, stable across three trials: the stdin-drain
# loop (`read -t 1`, one attempt against empty stdin), the fast path's own
# cooldown-timestamp read (`read -r LAST_TS < last-save-ts`), and
# lib-env-cache.sh's replay of its own 14-line cache file (14 successful
# reads, one that hits EOF) = 17. Mostly fixed by construction --
# lib-env-cache.sh always writes exactly those 14 lines
# (scripts/lib-env-cache.sh, the _remember_env_cache_publish printf block)
# regardless of config content -- so most of this is not a measurement that
# drifts with fixture data the way spawn counts can drift across platforms.
# The slack is the same width and for the same reason as
# FAST_PATH_SPAWN_BUDGET.
#
# A real limitation, not covered by the slack: xtrace shares fd 2 with the
# script's own stderr, so any code that redirects stderr (`... 2>/dev/null`,
# common in this file's own error handling) makes every `read` traced
# *inside* that redirected scope invisible to this pin, budget or no budget.
# See the docstring on test_an_extra_builtin_file_read_is_invisible_to_the_
# spawn_pin_but_caught_by_the_read_pin for the reproduction that surfaced
# this and why its fixture deliberately does not redirect stderr.
READ_BUILTIN_MEASURED = 17
READ_BUILTIN_BUDGET = READ_BUILTIN_MEASURED + 2


def _traced_warm_run(env: dict, plugin: Path, remember: Path, tmp_path: Path,
                      label: str):
    """Run the (possibly mutated) post-tool hook under `bash -x`, on a PATH
    that shims every counted external command, so one execution yields both
    the existing spawn-count evidence and the new read-builtin evidence.

    The fast path redirects its OWN fd 2 to `$REMEMBER_DIR/logs/hook-errors.log`
    once that directory exists (post-tool-hook.sh:176) -- which it does by the
    time this fixture's priming run has finished, exactly as it does for a real
    user. `bash -x` writes its trace to whatever fd 2 currently is, so once
    that redirect fires the rest of the trace lands in the log file, not in
    the subprocess.stderr this harness captures. Read it back and append it,
    so a `read` after the redirect is counted exactly like one before it.
    """
    log = tmp_path / ("spawns-" + label + ".log")
    shims = make_shim_dir(tmp_path)
    log.write_text("", encoding="utf-8")
    run_env = {**env, "SPAWN_LOG": str(log),
               "PATH": str(shims) + os.pathsep + env["PATH"]}
    err_log = remember / "logs" / "hook-errors.log"
    if err_log.exists():
        err_log.unlink()
    result = subprocess.run(
        ["bash", "-x", str(plugin / "scripts" / "post-tool-hook.sh")],
        env=run_env, input=b"", capture_output=True, timeout=120, check=False,
    )
    assert result.returncode == 0, repr(result.stderr[-800:])
    if err_log.exists():
        result.stderr = result.stderr + err_log.read_bytes()
    return result, _spawn_lines(log)


def _cmds(lines):
    return [line.split(" ", 1)[0] for line in lines]


def _measure(env: dict, plugin: Path, remember: Path, tmp_path: Path, label: str):
    """The one call site every MEASURED (non-priming) run in this file goes
    through. Brackets `_traced_warm_run` with an `EnvCacheProbe` and fails
    loudly, naming which run went cold and why, instead of silently
    reporting a cold-path spawn/read count as though it were the warm one
    this file claims to pin (see the module docstring's `#303` note -- this
    is the fix for the macOS-CI-only red that shipped without it)."""
    probe = EnvCacheProbe(env["TMPDIR"])
    probe.snapshot()
    result, spawns = _traced_warm_run(env, plugin, remember, tmp_path, label)
    probe.assert_warm("the " + label + " measurement in test_hot_path_cost_pin_330.py")
    return result, spawns


# -- The baseline: both pins, on the SHIPPED hook --------------------------

def test_the_warm_path_stays_inside_both_budgets(tmp_path):
    plugin = _scratch_plugin(tmp_path)
    home, project, remember = _project(tmp_path)
    env = _env(tmp_path, home, project, plugin)
    _prime(env, plugin)

    _traced_warm_run(env, plugin, remember, tmp_path, "cold")
    result, warm_spawns = _measure(env, plugin, remember, tmp_path, "warm")
    _reap(remember)

    reads = _read_builtin_lines(result.stderr)
    # POSITIVE CONTROL: the harness sees builtin reads at all. A count of
    # zero here is indistinguishable from "the trace was never captured" --
    # the same failure #298/#299 keep recurring as.
    assert len(reads) > 0, (
        "no `read` builtin observed on a run that must drain stdin and "
        "replay the env cache -- the xtrace harness is not seeing anything"
    )
    assert len(reads) <= READ_BUILTIN_BUDGET, (
        f"{len(reads)} builtin `read` invocations on a warm tool call "
        f"(budget {READ_BUILTIN_BUDGET}):\n  " + "\n  ".join(reads)
    )
    assert len(warm_spawns) <= FAST_PATH_SPAWN_BUDGET, (
        f"{len(warm_spawns)} external spawns on a warm tool call:\n  "
        + "\n  ".join(warm_spawns)
    )
    assert "git" not in _cmds(warm_spawns)


# -- The path #353 actually changed: a save has already landed (#395) -----

# Measured on macOS bash 3.2.57, stable across three trials, with a fixture
# carrying both last-save.json and its #353 sidecar (position inside this
# run's own transcript, so SIDECAR_TRUSTED is set and `pipeline.shell
# read-position` is never spawned): the same 17 reads
# test_the_warm_path_stays_inside_both_budgets measures on the "no save
# yet" path, PLUS the sidecar's own `read -r _SIDECAR_LINE < "$SIDECAR"`
# (post-tool-hook.sh:530) = 18. Confirmed against the harness itself, not
# guessed: identical to the round-one release audit's own count for #395.
#
# No slack, unlike READ_BUILTIN_BUDGET above. #395 exists because the "no
# save yet" pin above passed while ONE slack unit stood between this
# branch's own 18 measured reads and a budget of 19 that never actually
# walked it -- slack absorbed a gap nothing was pinning. Setting this one
# at the measured value means a future read added to the sidecar branch
# has to argue for itself in its own PR, rather than fitting inside a
# margin nobody is watching.
SIDECAR_READ_BUILTIN_MEASURED = 18
SIDECAR_READ_BUILTIN_BUDGET = SIDECAR_READ_BUILTIN_MEASURED


def test_the_warm_path_with_a_landed_save_stays_inside_both_budgets(tmp_path):
    """The path #353 actually changed: a prior save has landed, so the hot
    path consults the #353 sidecar instead of falling through to
    `pipeline.shell read-position`. #395: the existing budget case above
    never walks this branch at all (`_project` creates neither
    last-save.json nor a sidecar), so nothing pinned it -- this is the
    fixture and the budget that do.
    """
    plugin = _scratch_plugin(tmp_path)
    home, project, remember = _project_with_prior_save(tmp_path)
    env = _env(tmp_path, home, project, plugin)
    _prime(env, plugin)

    _traced_warm_run(env, plugin, remember, tmp_path, "cold-sidecar")
    result, warm_spawns = _measure(env, plugin, remember, tmp_path, "warm-sidecar")
    _reap(remember)

    reads = _read_builtin_lines(result.stderr)
    # POSITIVE CONTROL, part 1: the harness sees builtin reads at all.
    assert len(reads) > 0, (
        "no `read` builtin observed on a warm run with a landed save -- "
        "the xtrace harness is not seeing anything"
    )
    # POSITIVE CONTROL, part 2: the fixture actually walks the sidecar
    # branch, not just the same "no save yet" path under a new name. If
    # this is empty, the budget below measures nothing #395 reports as
    # unpinned.
    sidecar_reads = [r for r in reads if "_SIDECAR_LINE" in r]
    assert sidecar_reads, (
        "the sidecar's own `read -r _SIDECAR_LINE` was never observed -- "
        "this fixture is not reaching the #353 branch #395 reports as "
        "unpinned:\n  " + "\n  ".join(reads)
    )
    assert len(reads) <= SIDECAR_READ_BUILTIN_BUDGET, (
        f"{len(reads)} builtin `read` invocations on a warm tool call with "
        f"a landed save (budget {SIDECAR_READ_BUILTIN_BUDGET}):\n  "
        + "\n  ".join(reads)
    )
    assert len(warm_spawns) <= FAST_PATH_SPAWN_BUDGET, (
        f"{len(warm_spawns)} external spawns on a warm tool call with a "
        f"landed save:\n  " + "\n  ".join(warm_spawns)
    )
    assert "git" not in _cmds(warm_spawns)
    # The spawn drop #353 claims and #395 found unpinned: a trusted
    # sidecar must skip `pipeline.shell read-position` entirely, not just
    # stay under the shared spawn budget by coincidence.
    assert not any("read-position" in line for line in warm_spawns), (
        "a trusted sidecar must skip the `pipeline.shell read-position` "
        "spawn entirely:\n  " + "\n  ".join(warm_spawns)
    )


def test_a_defeated_sidecar_trips_the_read_position_spawn_check(tmp_path):
    """Construct the failure the budget case above exists to catch, per
    #395's own bar: would it still pass if the sidecar branch did
    nothing? Mutate the scratch hook so `SIDECAR_TRUSTED` is never set
    even when the sidecar agrees with last-save.json, and show that a
    warm run with a landed save then falls through to the
    `pipeline.shell read-position` spawn #353 exists to avoid -- proof
    the pin above would have failed on this regression, not merely that
    it passes on the shipped hook.
    """
    plugin = _scratch_plugin(tmp_path)
    _patch(plugin,
           '                LAST_LINE=$((10#$_SIDECAR_LINE))\n'
           '                SIDECAR_TRUSTED=1\n',
           '                LAST_LINE=$((10#$_SIDECAR_LINE))\n'
           '                : # SIDECAR_TRUSTED deliberately not set (#395 regression fixture)\n')
    home, project, remember = _project_with_prior_save(tmp_path)
    env = _env(tmp_path, home, project, plugin)
    _prime(env, plugin)

    _traced_warm_run(env, plugin, remember, tmp_path, "prime-defeated-sidecar")
    _result, warm_spawns = _measure(env, plugin, remember, tmp_path, "defeated-sidecar")
    _reap(remember)

    assert any("read-position" in line for line in warm_spawns), (
        "the injected mutation did not defeat sidecar trust -- this "
        "fixture is not reproducing the regression it claims to:\n  "
        + "\n  ".join(warm_spawns)
    )


# -- Reintroducing #298/#299: wrapper spawns, real git literal hidden -----

@pytest.mark.parametrize("wrapper,label", [
    ('GIT=git\n"$GIT" -C "$REMEMBER_DIR" rev-parse --show-toplevel >/dev/null 2>&1\n',
     "variable-wrapper"),
    # Quoted rather than `command git -C ...`: the bare form spells the
    # literal substring `"git "` (g-i-t-SPACE) that the substring check in
    # test_case_divergence_298.py already refuses, so it would prove nothing
    # about the gap #330 reports. Quoting the command name is a real evasion
    # -- it still bypasses a shell function or alias named `git`, which is
    # the whole point of `command` -- while never producing that four-byte
    # run: `"git"` is followed by a closing quote, not a space.
    ('command "git" -C "$REMEMBER_DIR" rev-parse --show-toplevel >/dev/null 2>&1\n',
     "command-git"),
])
def test_a_git_wrapper_on_the_hot_path_trips_the_spawn_pin(tmp_path, wrapper, label):
    """#330's own examples: `GIT=git; "$GIT" -C ...` and `command git` both
    reach the real `git` binary while never spelling the literal `"git "`
    that tests/test_case_divergence_298.py's substring check looks for.
    Proof that the EXISTING spawn budget in test_post_tool_fast_path_350.py
    is not vacuous against them -- reproduced against a scratch copy rather
    than asserted about."""
    plugin = _scratch_plugin(tmp_path)
    _patch(plugin, 'PLUGIN_ROOT="$PIPELINE_DIR"',
           wrapper + 'PLUGIN_ROOT="$PIPELINE_DIR"')
    home, project, remember = _project(tmp_path)
    env = _env(tmp_path, home, project, plugin)
    _prime(env, plugin)

    _traced_warm_run(env, plugin, remember, tmp_path, "prime-" + label)
    _result, warm_spawns = _measure(env, plugin, remember, tmp_path, label)
    _reap(remember)

    body = (plugin / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    assert "git " not in code, (
        "the wrapper this test installs must NOT contain the literal "
        "test_case_divergence_298.py checks for, or this proves nothing "
        "about the gap #330 reports"
    )
    assert "git" in _cmds(warm_spawns), (
        f"the {label} wrapper did not reach the real git binary -- this "
        "fixture is not reproducing #330's scenario. Spawned:\n  "
        + "\n  ".join(warm_spawns)
    )


# -- Reintroducing #298/#299: a builtin read, invisible to spawn counting -

def test_an_extra_builtin_file_read_is_invisible_to_the_spawn_pin_but_caught_by_the_read_pin(tmp_path):
    """The literal gap #330 names: "an extra file read... not even in the
    substring set's vocabulary" -- and "a loop" is named right next to it.
    It is not in the spawn-count vocabulary either -- a `read` builtin forks
    nothing. Reproduced by LOOPING a real `read` over a real file already on
    the hot path (`config.json`, five times), the cheapest way to add cost
    with no new process and no new literal.

    Deliberately NOT `2>/dev/null` on the injected loop, unlike several real
    reads elsewhere in this file. `bash -x` writes its trace to whatever fd 2
    currently is, so a defect that redirects its own stderr -- which is
    common in this codebase's own error handling -- would make the read(s)
    inside it invisible to this harness too. That is a real, named limit of
    an xtrace-based pin, not one this fixture is built to hide.
    """
    plugin = _scratch_plugin(tmp_path)
    extra_read = (
        '_j=0\n'
        'while [ "$_j" -lt 5 ]; do\n'
        '    _extra=""\n'
        '    while IFS= read -r _extra || [ -n "$_extra" ]; do :; done '
        '< "$REMEMBER_DIR/config.json"\n'
        '    _j=$(( _j + 1 ))\n'
        'done\n'
    )
    _patch(plugin, 'PLUGIN_ROOT="$PIPELINE_DIR"',
           extra_read + 'PLUGIN_ROOT="$PIPELINE_DIR"')
    home, project, remember = _project(tmp_path)
    env = _env(tmp_path, home, project, plugin)
    _prime(env, plugin)

    _traced_warm_run(env, plugin, remember, tmp_path, "prime-read")
    result, warm_spawns = _measure(env, plugin, remember, tmp_path, "extra-read")
    _reap(remember)

    body = (plugin / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    for literal in ("lib-case-divergence", "case_divergence", "git "):
        assert literal not in code, (
            f"the injected defect accidentally contains {literal!r} -- it "
            "would be caught by the substring test for the wrong reason, "
            "which is not what this test claims to demonstrate"
        )
    assert len(warm_spawns) <= FAST_PATH_SPAWN_BUDGET, (
        "the injected read spawned a process -- this fixture is not "
        "reproducing a spawn-free defect. Spawned:\n  " + "\n  ".join(warm_spawns)
    )

    reads = _read_builtin_lines(result.stderr)
    assert len(reads) > READ_BUILTIN_BUDGET, (
        f"only {len(reads)} builtin reads observed (budget {READ_BUILTIN_BUDGET}) "
        "-- the injected extra read was not counted, so this pin would not "
        "have caught #330's own example"
    )


# -- The stated boundary: what no pin here can see -------------------------

def test_a_pure_compute_loop_is_invisible_to_every_pin_here(tmp_path):
    """Named rather than hidden: a loop that spawns nothing and reads
    nothing adds real per-call cost and neither pin above moves. This is
    the honest edge of what a spawn-count-plus-read-count harness can pin --
    catching it would need wall-clock or instruction counting, and #330
    explicitly allows "this cannot be made robust" as a valid outcome for
    whatever it does not cover."""
    plugin = _scratch_plugin(tmp_path)
    compute_loop = 'for _i in 1 2 3 4 5 6 7 8 9 10; do : $(( _i * _i )); done\n'
    _patch(plugin, 'PLUGIN_ROOT="$PIPELINE_DIR"',
           compute_loop + 'PLUGIN_ROOT="$PIPELINE_DIR"')
    home, project, remember = _project(tmp_path)
    env = _env(tmp_path, home, project, plugin)
    _prime(env, plugin)

    _traced_warm_run(env, plugin, remember, tmp_path, "prime-compute")
    result, warm_spawns = _measure(env, plugin, remember, tmp_path, "compute")
    _reap(remember)

    reads = _read_builtin_lines(result.stderr)
    assert len(warm_spawns) <= FAST_PATH_SPAWN_BUDGET
    assert len(reads) <= READ_BUILTIN_BUDGET, (
        "a pure compute loop was expected to pass both budgets silently -- "
        "if it tripped one of them, the claim in this test's docstring is "
        "wrong and needs correcting, not the assertion"
    )
