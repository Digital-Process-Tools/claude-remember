"""The shell parse gate: nothing shipped may fail `bash -n` (#302).

A syntax error in a hook makes it exit non-zero, and on `UserPromptSubmit` a
non-zero exit **discards the prompt the user just typed**. #301 produced exactly
that — a `case` inside the hook's `$( )` substitution — and it was caught only
because that one file happens to be well tested. Nothing in the suite asserted
that the shipped shell *parses*, so a quieter hook under `hooks.d/` would have
shipped the same error to a user whose typing then vanished.

Three states, per this repo's contract (`test_repo_mutation_guard.py`,
`test_pep604_floor_guard.py`): `ok`, `violations`, `skipped`. And one thing this
gate has to be more careful about than most: **an `ok` here is scoped to the
interpreters that were actually present**. The claim "bash 3.2 was among them"
is made by exactly one test below, which skips loudly — naming the constructs it
could not check — rather than letting a modern-bash pass stand in for it.

See `tests/shell_parse.py` for what the gate deliberately cannot catch.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path
from typing import List

import pytest

from tests.shell_parse import (
    KIND_BASH,
    KIND_SH,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_VIOLATIONS,
    Interpreter,
    discover_interpreters,
    repo_shell_files,
    required_kind,
    run_gate,
    shebang_interpreter,
)

_ROOT = Path(__file__).resolve().parent.parent

# The `case`-inside-`$( )` construct from #301, verbatim in shape. Bash's parser
# reads the `)` closing the `a)` pattern as the end of the substitution.
_ISSUE_301_CONSTRUCT = """#!/bin/bash
value=$(
case "$1" in
  a) echo a ;;
esac
)
echo "$value"
"""

# Broken under every bash ever shipped — an unterminated function body.
_UNIVERSALLY_BROKEN = "#!/bin/bash\nmain() {\n  echo hi\n"


@functools.lru_cache(maxsize=1)
def _repo_report():
    """One run of the gate over this repository, shared by every test below.

    The gate spawns one process per (file, interpreter) pair. Five tests asking
    the same question five times would be five times the spawns for one answer.
    """
    return run_gate(_ROOT)


def _interpreters() -> List[Interpreter]:
    return discover_interpreters()


def _floor_bashes() -> List[Interpreter]:
    return [i for i in _interpreters() if i.kind == KIND_BASH and i.is_floor]


# ── The gate, run against this repository ─────────────────────────────────

def test_every_shipped_shell_script_parses() -> None:
    report = _repo_report()

    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    assert report.status == STATUS_OK, "\n".join(
        ["{} — shipped shell that does not parse:".format(report.reason)]
        + [
            "  {} under {}\n    {}".format(
                e.path.relative_to(_ROOT).as_posix(), e.interpreter, e.message
            )
            for e in report.errors
        ]
    )


def test_the_gate_states_which_interpreters_it_used() -> None:
    """The enumeration is the point. An `ok` that does not say what ran is the
    defect this gate exists to prevent, one level up."""
    report = _repo_report()
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    offered = [i.label for i in report.interpreters]
    used = [i.label for i in report.used]
    assert used, "the gate reported a result without naming a single interpreter"
    assert set(used) <= set(offered), (used, offered)
    print("")
    print("offered:      {}".format(", ".join(offered)))
    print("\nused:         {}".format(", ".join(used)))
    print("files:        {}".format(report.files_scanned))
    print("floor bash:   {}".format("covered" if report.floor_covered else "NOT covered"))
    for gap in report.gaps:
        print("gap:          {}".format(gap))


def test_the_floor_bash_actually_ran_or_the_gap_is_named() -> None:
    """The only test entitled to claim bash 3.2 coverage.

    Stock macOS ships bash 3.2 and CI runners ship 5.x, so on most machines this
    skips. It skips *loudly*, naming what went unchecked, rather than letting
    `test_every_shipped_shell_script_parses` pass for coverage it did not have.
    """
    report = _repo_report()
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    if not report.floor_covered:
        floor_gaps = [g for g in report.gaps if "below" in g]
        pytest.skip(floor_gaps[0] if floor_gaps else "no floor bash available")

    assert report.status == STATUS_OK, report.reason


def test_nothing_shipped_is_silently_left_unchecked() -> None:
    """A gate that skips a file it cannot classify is the same defect it exists
    to prevent, so the skip list must be empty."""
    report = _repo_report()
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    assert report.unchecked == (), [
        (p.relative_to(_ROOT).as_posix(), why) for p, why in report.unchecked
    ]


# ── Scope of the walk: it must not pass by checking nothing ───────────────

def test_the_walk_covers_every_shell_file_git_tracks() -> None:
    """The direct answer to "would this pass if it checked nothing?".

    git is the second opinion: any directory the walk quietly drops — including
    a dot-directory like `.github/` — shows up here as a tracked `.sh` the walk
    did not return.
    """
    tracked = subprocess.run(
        ["git", "-C", str(_ROOT), "ls-files", "*.sh"],
        capture_output=True, text=True, timeout=30,
    )
    if tracked.returncode != 0:
        pytest.skip("not a git checkout — cannot cross-check the walk")

    expected = {line for line in tracked.stdout.splitlines() if line.strip()}
    walked = {p.relative_to(_ROOT).as_posix() for p in repo_shell_files(_ROOT)}

    assert expected, "git reports no tracked shell files, which cannot be right"
    assert expected <= walked, sorted(expected - walked)


def test_the_walk_names_the_files_that_would_erase_a_prompt() -> None:
    walked = {p.relative_to(_ROOT).as_posix() for p in repo_shell_files(_ROOT)}

    assert "scripts/user-prompt-hook.sh" in walked
    assert "scripts/post-tool-hook.sh" in walked
    assert "scripts/session-start-hook.sh" in walked
    assert "hooks.d/after_save/50-git-backup.sh" in walked
    assert "hooks.d/before_session_start/50-git-restore.sh" in walked
    assert len(walked) > 15, sorted(walked)


def test_the_walk_excludes_machine_state_that_sits_in_the_working_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "real.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    for junk in (".venv/bin/activate.sh", "node_modules/m/i.sh", "build/x.sh",
                 ".git/hooks/pre-commit.sh"):
        path = root / junk
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_UNIVERSALLY_BROKEN, encoding="utf-8")

    walked = {p.relative_to(root).as_posix() for p in repo_shell_files(root)}
    assert walked == {"scripts/real.sh"}, walked


# ── Detection: the gate has to be able to go red ──────────────────────────

def _stage(tmp_path: Path, name: str, body: str) -> Path:
    root = tmp_path / "repo"
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return root


def test_a_broken_script_is_a_violation_not_a_pass(tmp_path: Path) -> None:
    root = _stage(tmp_path, "scripts/broken.sh", _UNIVERSALLY_BROKEN)
    report = run_gate(root)
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    assert report.status == STATUS_VIOLATIONS, report.reason
    assert [e.path.name for e in report.errors] == ["broken.sh"] * len(report.errors)
    assert "syntax error" in report.errors[0].message


def test_the_issue_301_construct_is_caught_by_the_floor_bash(tmp_path: Path) -> None:
    """`case` inside `$( )` parses fine on bash 5 and fails on bash 3.2.

    Which is the entire argument for running the gate under the floor rather
    than under whatever is first on PATH: a modern-bash-only gate reports `ok`
    on the exact construct that erased a prompt in #301.
    """
    floor = _floor_bashes()
    if not floor:
        pytest.skip(
            "no bash below 4.0 on this machine — the #301 construct cannot be "
            "demonstrated, and by the same token would not be caught here"
        )

    root = _stage(tmp_path, "scripts/hook.sh", _ISSUE_301_CONSTRUCT)
    report = run_gate(root, interpreters=floor)

    assert report.status == STATUS_VIOLATIONS, report.reason
    assert "syntax error" in report.errors[0].message


def test_a_clean_script_is_not_flagged(tmp_path: Path) -> None:
    root = _stage(
        tmp_path,
        "scripts/fine.sh",
        '#!/bin/bash\nset -u\nmain() {\n  case "$1" in\n    a) echo a ;;\n  esac\n}\nmain "$@"\n',
    )
    report = run_gate(root)
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    assert report.status == STATUS_OK, [e.message for e in report.errors]
    assert report.files_scanned == 1


# ── Shebang dispatch: the right grammar, or an audible refusal ────────────

def test_the_shebang_selects_the_grammar(tmp_path: Path) -> None:
    root = tmp_path
    bash_file = root / "b.sh"
    bash_file.write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    env_file = root / "e.sh"
    env_file.write_text("#!/usr/bin/env bash\ntrue\n", encoding="utf-8")
    sh_file = root / "s.sh"
    sh_file.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")

    assert shebang_interpreter(bash_file) == "bash"
    assert shebang_interpreter(env_file) == "bash"
    assert required_kind(bash_file) == (KIND_BASH, None)
    assert required_kind(env_file) == (KIND_BASH, None)
    assert required_kind(sh_file) == (KIND_SH, None)


def test_a_missing_shebang_is_an_assumption_stated_out_loud(tmp_path: Path) -> None:
    """Sourced libraries legitimately have no shebang. Checking them as bash is
    right here — every sourcer in this tree is bash — but it is an assumption,
    so it is recorded rather than buried."""
    library = tmp_path / "lib.sh"
    library.write_text("greet() { echo hi; }\n", encoding="utf-8")
    kind, note = required_kind(library)

    assert kind == KIND_BASH
    assert note and "assumed bash" in note

    root = _stage(tmp_path, "scripts/lib.sh", "greet() { echo hi; }\n")
    report = run_gate(root)
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)
    assert report.status == STATUS_OK
    assert [p.name for p in report.assumptions] == ["lib.sh"]


def test_a_non_shell_shebang_is_reported_not_quietly_passed(tmp_path: Path) -> None:
    root = _stage(tmp_path, "scripts/odd.sh", "#!/usr/bin/env python3\nx = (\n")
    report = run_gate(root)
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    assert report.unchecked, "a file the gate cannot check must be named, not skipped"
    assert report.unchecked[0][0].name == "odd.sh"
    assert any("odd.sh" in gap for gap in report.gaps)


# ── Skipped is not a pass ─────────────────────────────────────────────────

def test_a_tree_with_no_shell_files_is_skipped_not_passed(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    report = run_gate(root)

    assert report.status == STATUS_SKIPPED
    assert "not the same as finding nothing wrong" in report.reason


def test_no_bash_at_all_is_skipped_not_passed(tmp_path: Path) -> None:
    root = _stage(tmp_path, "scripts/x.sh", _UNIVERSALLY_BROKEN)
    report = run_gate(root, interpreters=[])

    assert report.status == STATUS_SKIPPED
    assert "no bash interpreter found" in report.reason
    assert report.floor_covered is False


def test_a_modern_only_run_reports_the_floor_gap() -> None:
    """Synthesised because the machine that runs this may have only one bash.

    The point being asserted is that the gap is *stated*, not that the machine
    is missing anything: a run under bash 5 alone must not present itself as
    coverage of the 3.2 a user's Mac has.
    """
    real = [i for i in _interpreters() if i.kind == KIND_BASH]
    if not real:
        pytest.skip("no bash on this machine")
    modern = Interpreter(real[0].path, KIND_BASH, (5, 2))
    report = run_gate(_ROOT, interpreters=[modern])
    if report.status == STATUS_SKIPPED:
        pytest.skip(report.reason)

    assert report.floor_covered is False
    assert any("below 4.0" in gap for gap in report.gaps), report.gaps
