"""#898 round 8: the shipped shell scripts themselves, read the way the
directory's offline sweep (claude-directory-publishing tools/sweep.sh) and
its port in .github/scripts/check_release_tree.py read them.

The release build (#900) inlines the sourced libraries into each hook, so a
library whose text leaves the scanner's loop counter open -- a multi-line
program in a quoted string, whose own `for`/`while` lines read as shell
loops -- turns every later catch-all `*)` in the compiled hook into a
"catch-all inside a loop" hit. These tests read the SOURCE files, so they
run here without the compile step.

Every negative assertion is paired with a positive control on a synthetic
file that does carry the shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"
# Developer-only scripts the release build leaves out of the tree.
NOT_SHIPPED = {"bench-slug.sh", "run-tests.sh"}
SHIPPED_SH = sorted(p for p in (REPO_ROOT / "scripts").glob("*.sh") if p.name not in NOT_SHIPPED)
# #898 round 9: the sweep-shape guards also cover hooks.d -- the opt-in git
# backup/restore hooks ship in the release tree beside scripts/.
SHAPE_SH = SHIPPED_SH + sorted((REPO_ROOT / "hooks.d").rglob("*.sh"))

PROBE = 'case "$probe" in\n    *) : ;;\nesac\n'


def _load():
    spec = importlib.util.spec_from_file_location("check_release_tree_src898", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree_src898"] = mod
    spec.loader.exec_module(mod)
    return mod


def _catch_all_hits(text: str) -> list:
    mod = _load()
    off: list = []
    files = {"scripts/x.sh": text.encode("utf-8")}
    mod._check_catch_all_in_loop(files, {"scripts/x.sh": "text"}, off)
    return off


def _probe_flagged(text: str) -> bool:
    """True when a catch-all arm appended AFTER the file's own text reads as
    sitting inside a loop -- i.e. the file leaves the loop counter open."""
    if not text.endswith("\n"):
        text += "\n"
    probe_line = text.count("\n") + 2  # the `*)` line of PROBE
    return any(h.startswith(f"scripts/x.sh:{probe_line}:") for h in _catch_all_hits(text + PROBE))


def test_probe_is_flagged_after_an_unclosed_loop():
    """Positive control: a file whose text leaves a `for` open (here, a
    Python program in a single-quoted here-string) does flag the probe."""
    leaky = "python3 - <<< 'import sys\nfor a in sys.argv:\n    print(a)\n'\n"
    assert _probe_flagged(leaky)


def test_probe_is_not_flagged_after_a_closed_loop():
    assert not _probe_flagged("for a in 1 2; do\n    :\ndone\n")


@pytest.mark.parametrize("path", SHIPPED_SH, ids=lambda p: p.name)
def test_shipped_script_leaves_the_loop_counter_closed(path):
    assert SHIPPED_SH, "no shipped scripts found -- the glob is broken"
    assert not _probe_flagged(path.read_text(encoding="utf-8")), (
        f"{path.name} leaves the scanner's loop counter open at end of file; "
        "once the build inlines it into a hook, every later catch-all `*)` "
        "reads as inside a loop. Move multi-line programs out of quoted "
        "strings into their own files.")


def _shape_hits(check_name: str, text: str) -> list:
    mod = _load()
    hits: list = []
    files = {"scripts/x.sh": text.encode("utf-8")}
    getattr(mod, check_name)(files, {"scripts/x.sh": "text"}, hits)
    return hits


SHAPE_CHECKS = {
    # check function -> a line that carries the shape (positive control)
    "_check_escaped_quote": 'echo "say \\"hi\\""\n',
    "_check_slash_glob_case": 'case "$0" in */*) : ;; esac\n',
    # #898 round 9 (triggers.md 9): a quoted literal inside a case pattern.
    "_check_quoted_literal_case": 'case "$x" in\n    *"not logged in"*) : ;;\nesac\n',
    # `${!name}` and `${!arr[@]}` alike -- the portal cited both as "reads an
    # environment variable named at run time" (triggers.md).
    "_check_indirect_expansion": 'v="${!slot:-}"\n',
    # A quoted lone dot, either quote style.
    "_check_dot_string": 'ROOT="."\n',
    # #898 round 10: argv assembled at run time (MCP_FORWARDS_CREDENTIAL_ENV's
    # send side, "a command assembled at run time").
    "_check_runtime_argv": 'git -C "$d" push -- "$r" ${b:+"$b"}\n',
    # #898 round 10, the bare "." in COMMAND_SCRIPT_NOT_FOLLOWED's file list:
    # a lone `.`/`..` word right after `|`, `;` or `(` -- a case alternative
    # (`""|.|..|-*)`) or a jq/awk program's `; . * $x` -- reads as a `.`
    # (source) command to a line scanner.
    "_check_bare_dot_word": 'case "$1" in\n    ""|.|..|-*) : ;;\nesac\n',
    # A backslash glob in a case pattern (`[A-Za-z]:` then two backslashes),
    # batch-cleared on jit-context alongside the bare dot.
    "_check_backslash_case_pattern":
        'case "$p" in\n    [A-Za-z]:' + "\x5c" * 2 + ') : ;;\nesac\n',
    # #898 round 11 (triggers.md 10): a plain copy of the plugin-root
    # variable reads as a bare "." under COMMAND_SCRIPT_NOT_FOLLOWED.
    "_check_plugin_root_copy": 'PIPELINE_DIR="$CLAUDE_PLUGIN_ROOT"\n',
}


@pytest.mark.parametrize("check_name", sorted(SHAPE_CHECKS))
def test_shape_check_fires_on_its_own_shape(check_name):
    """Positive control for the per-file assertions below: the harness
    reaches the check and the check fires."""
    assert _shape_hits(check_name, SHAPE_CHECKS[check_name])


@pytest.mark.parametrize("check_name", sorted(SHAPE_CHECKS))
@pytest.mark.parametrize("path", SHAPE_SH, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shipped_script_carries_no_sweep_shape(path, check_name):
    hits = _shape_hits(check_name, path.read_text(encoding="utf-8"))
    assert not hits, f"{path.name}: {hits}"


# #898 round 10: every sub-shape the run-time-argv check reads, each a
# positive control of its own, with the literal rewrite of each as the
# matching negative -- the rewrite this round shipped must not still fire.
RUNTIME_ARGV = {
    "conditional word": ('git push -- "$r" ${b:+"$b"}\n',
                         'git push -- "$r" "$b"\n'),
    "unquoted variable in git argv": ('git -C "$d" commit $FLAG -m x\n',
                                      'git -C "$d" commit --no-gpg-sign -m x\n'),
    "eval": ('eval "x_${s}=\\$y"\n',
             'x_slots[i]="$y"\n'),
    "default holding an expansion": ('export C="${C:-ssh -oT=$T}"\n',
                                     "printf -v C 'ssh -oT=%s' \"$T\"\n"),
}


@pytest.mark.parametrize("shape", sorted(RUNTIME_ARGV))
def test_runtime_argv_fires_on_each_shape(shape):
    assert _shape_hits("_check_runtime_argv", RUNTIME_ARGV[shape][0])


@pytest.mark.parametrize("shape", sorted(RUNTIME_ARGV))
def test_runtime_argv_clears_the_literal_rewrite(shape):
    assert not _shape_hits("_check_runtime_argv", RUNTIME_ARGV[shape][1])
