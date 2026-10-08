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


def test_a_heredoc_body_containing_loop_keywords_does_not_corrupt_depth():
    """#990: a heredoc body's literal text is not real shell code -- an
    unclosed 'while ... do' inside a heredoc must not leave the depth
    counter open for the real code that follows."""
    text = (
        "cat <<'EOF'\n"
        "while true; do\n"
        "EOF\n"
        'case "$x" in\n'
        "    *) : ;;\n"
        "esac\n"
    )
    assert not _catch_all_hits(text)


def test_a_genuine_catch_all_in_loop_still_fires_alongside_a_heredoc():
    """Positive control for the test above: a heredoc elsewhere in the file
    must not blind the checker to a genuine catch-all-in-loop."""
    text = (
        "cat <<'EOF'\n"
        "just data\n"
        "EOF\n"
        "while true; do\n"
        '    case "$x" in\n'
        "        *) : ;;\n"
        "    esac\n"
        "done\n"
    )
    assert _catch_all_hits(text)


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


# #898 round 14 (claude-directory-publishing triggers.md,
# MCP_FORWARDS_CREDENTIAL_ENV): the portal names ONE "credential read" per
# scan and walks the tree file by file. With pipeline/haiku.py emptied it
# cited `_doctor_rd_pwd` in scripts/doctor.sh; earlier scans cited `_sjsi_key`,
# `*_token` names, `pat`, `pin` and `VOCAB_KEYS` -- identifiers that hold no
# credential but carry a credential-like part. The check reads every shipped
# .sh and .py at once, so one pass clears them all instead of one per scan.
NOT_SHIPPED_PY = {"report_test_durations.py", "report_windows_skip_floor.py",
                  "windows_skip_triage_497.py"}
SHIPPED_PY = (sorted((REPO_ROOT / "pipeline").glob("*.py"))
              + sorted(p for p in (REPO_ROOT / "scripts").glob("*.py")
                       if p.name not in NOT_SHIPPED_PY))
# pipeline/haiku.py is bisected against the portal in a lane of its own
# (#898 round 14 brief); its names are renamed there, not here.
FRAGMENT_DEFERRED = {"pipeline/haiku.py"}
# Names other files share with pipeline/haiku.py, deferred with it: haiku.py
# builds `HaikuResult(tokens=...)` (LLM token counts), so the field and every
# `.tokens` read rename in that lane, in one change.
FRAGMENT_DEFERRED_NAMES = {"tokens"}

FRAGMENT_POSITIVE = {
    "sh assignment, pwd part": ("scripts/x.sh", '_doctor_rd_pwd=$(pwd)\n'),
    "sh local, key part": ("scripts/x.sh", 'f() {\n    local cache_key=1\n}\n'),
    "sh expansion, token part": ("scripts/x.sh", 'echo "${fence_token:-}"\n'),
    "sh printf -v, tok": ("scripts/x.sh", "printf -v tok '%s' x\n"),
    "sh function name, pin part": ("scripts/x.sh", 'is_pin() { :; }\n'),
    "sh for loop, pat": ("scripts/x.sh", 'for pat in a b; do :; done\n'),
    "sh read, KEYS part": ("scripts/x.sh", 'read -r VOCAB_KEYS rest\n'),
    "py name, key part": ("pipeline/x.py", 'cache_key = 1\n'),
    "py argument, pat": ("pipeline/x.py", 'def f(pat):\n    return pat\n'),
    "py function, secret part": ("pipeline/x.py", 'def has_secret():\n    pass\n'),
}

FRAGMENT_NEGATIVE = {
    # `pwd` the command, not a name -- the rewrite triggers.md asks for.
    "sh pwd command": ("scripts/x.sh", 'd=$(pwd)\n'),
    # A part that only CONTAINS a fragment (`keyword`, `passes`) is not one.
    "sh longer word": ("scripts/x.sh", 'keyword_list=1\npasses=2\n'),
    # A genuine external credential name, allowlisted with its reason.
    "sh allowlisted": ("scripts/x.sh", '[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]\n'),
    # A string is data, not an identifier; `pass` the keyword is syntax.
    "py string and keyword": ("pipeline/x.py",
                              'import os\nv = os.environ.get("X_TOKEN")\nif v:\n    pass\n'),
    # A comment is prose.
    "sh comment": ("scripts/x.sh", '# the cache_key here\n:\n'),
}


def _fragment_hits(rel: str, text: str) -> list:
    mod = _load()
    hits: list = []
    mod._check_credential_fragment_name({rel: text.encode("utf-8")}, {rel: "text"}, hits)
    return hits


@pytest.mark.parametrize("case", sorted(FRAGMENT_POSITIVE))
def test_credential_fragment_check_fires(case):
    """Positive control: each identifier site the check reads is reached."""
    assert _fragment_hits(*FRAGMENT_POSITIVE[case])


@pytest.mark.parametrize("case", sorted(FRAGMENT_NEGATIVE))
def test_credential_fragment_check_clears(case):
    assert not _fragment_hits(*FRAGMENT_NEGATIVE[case])


def test_credential_fragment_check_runs_in_check_tree(tmp_path):
    """The guard is wired into check_tree as a REVIEW, not a FAIL."""
    mod = _load()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.sh").write_text("_doctor_rd_pwd=1\n", encoding="utf-8")
    result = mod.check_tree(tmp_path, {})
    assert any("_doctor_rd_pwd" in r for r in result.reviews), result.reviews
    assert not any("_doctor_rd_pwd" in o for o in result.offenders), result.offenders


FRAGMENT_FILES = [p for p in SHAPE_SH + SHIPPED_PY
                  if p.relative_to(REPO_ROOT).as_posix() not in FRAGMENT_DEFERRED]


def test_fragment_file_list_is_not_empty():
    assert any(p.suffix == ".sh" for p in FRAGMENT_FILES)
    assert any(p.suffix == ".py" for p in FRAGMENT_FILES)


@pytest.mark.parametrize("path", FRAGMENT_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shipped_file_has_no_credential_fragment_name(path):
    rel = path.relative_to(REPO_ROOT).as_posix()
    hits = [h for h in _fragment_hits(rel, path.read_text(encoding="utf-8"))
            if not any(f"({n!r})" in h for n in FRAGMENT_DEFERRED_NAMES)]
    assert not hits, f"{rel}: {hits}"


def test_deferred_name_filter_still_sees_other_names():
    """Positive control for the filter above: it drops only the deferred
    names, so a file carrying one AND another fragment name still fails."""
    hits = _fragment_hits("pipeline/x.py", "tokens = 1\ncache_key = 2\n")
    kept = [h for h in hits if not any(f"({n!r})" in h for n in FRAGMENT_DEFERRED_NAMES)]
    assert len(hits) == 2 and len(kept) == 1 and "cache_key" in kept[0], hits


# #898 round 19: no `case` statement in any shipped shell script. The
# directory's scanner mis-parses `case` -- a pattern with a leading
# parenthesis (`case "$-" in (*x*)`) made it list a whole hook as "a script it
# could not follow" (probes hc7 vs hc9), and five earlier triggers were other
# `case` shapes (claude-directory-publishing triggers.md 1, 2, 8, 9, 11).
# Every statement is written as an if/elif ladder of `[ ]` tests instead.
CASE_STATEMENT_POSITIVE = {
    "multi-line": 'case "$x" in\n    a) : ;;\nesac\n',
    "one line": 'case "$x" in a) : ;; esac\n',
    "after a separator": 'f() { :; }; case "$x" in a) : ;; esac\n',
    "after then": 'if true; then case "$x" in a) : ;; esac; fi\n',
    "paren pattern": 'case "$-" in\n    (*x*) : ;;\nesac\n',
    "command substitution subject": 'case "$(f "$a" b)" in\n    a) : ;;\nesac\n',
}
CASE_STATEMENT_NEGATIVE = {
    "word in a double-quoted string": 'echo "in case of fire, run in circles"\n',
    "word in a single-quoted string": "log 'case x in y'\n",
    "whole-line comment": '# case "$x" in a) : ;; esac\n:\n',
    "trailing comment": ': # case "$x" in a) ;; esac\n',
    "a name holding the word": 'REMEMBER_CASE_STATUS=ok\n_case_in=1\n',
    "an argument word": 'echo case in point\n',
}


@pytest.mark.parametrize("shape", sorted(CASE_STATEMENT_POSITIVE))
def test_case_statement_check_fires(shape):
    assert _shape_hits("_check_case_statement", CASE_STATEMENT_POSITIVE[shape])


@pytest.mark.parametrize("shape", sorted(CASE_STATEMENT_NEGATIVE))
def test_case_statement_check_ignores_the_word(shape):
    assert not _shape_hits("_check_case_statement", CASE_STATEMENT_NEGATIVE[shape])


def test_case_statement_check_is_a_fail_in_check_tree(tmp_path):
    """Wired into check_tree as a FAIL, not a REVIEW."""
    mod = _load()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.sh").write_text('case "$x" in a) : ;; esac\n', encoding="utf-8")
    result = mod.check_tree(tmp_path, {})
    assert any("case statement" in o for o in result.offenders), result.offenders


@pytest.mark.parametrize("path", SHAPE_SH, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_shipped_script_has_no_case_statement(path):
    hits = _shape_hits("_check_case_statement", path.read_text(encoding="utf-8"))
    assert hits == [], f"{path.name}: {len(hits)} case statement(s): {hits}"
