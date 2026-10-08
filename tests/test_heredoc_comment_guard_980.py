"""#980: a heredoc-body line must not be read as ordinary comment-skipped
code by any checker in check_release_tree.py that decides "is this a
comment" with the bare `line.lstrip().startswith("#")` test and has no
heredoc-boundary tracking of its own -- the adjacent finding from #919's
lane self-review (PR #978), which fixed exactly one such checker
(`_check_url_in_comment`) and reported the others as "reasoned, not
verified".

Every "must not fire inside a heredoc body" case here is paired with a
"must still fire outside one" positive control, the same convention as
tests/test_release_branch_check_898.py and tests/test_release_branch_check_900.py
-- a broken tracker that swallows everything would otherwise pass the
negative half for free.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"

KIB = 1024
BUDGET = {"max_file_bytes": 256 * KIB, "max_files": 512, "max_total_bytes": 3 * KIB * KIB}


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#851)"
    spec = importlib.util.spec_from_file_location("check_release_tree_980", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree_980"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    base = {
        ".claude-plugin/plugin.json": (b'{"name": "x", "description": "d", '
                                       b'"version": "1.0.0", "author": {"name": "a"}}\n'),
        "README.md": ("# x\n\n" + " ".join(["word"] * 40) + "\n").encode(),
        "LICENSE": b"license\n",
        "scripts/run.sh": b"#!/bin/sh\necho hi\n",
        "hooks/hooks.json": json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh"},
        ]}]}}).encode(),
    }
    base.update(files)
    for rel, data in base.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _check(root: Path):
    mod = _load()
    return mod.check_tree(root, dict(BUDGET))


# -- _sh_lines (feeds 8 REVIEW/FAIL checkers at once: _check_escaped_quote,
# _check_slash_glob_case, _check_quoted_literal_case, _check_runtime_argv,
# _check_bare_dot_word, _check_backslash_case_pattern, _check_plugin_root_copy,
# _check_case_statement) --

def test_heredoc_body_bare_dot_word_is_not_flagged(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\ncat << EOF\n.\nEOF\n"})
    reviews = _check(root).reviews
    assert not any("dot word" in r for r in reviews), reviews


def test_bare_dot_word_outside_heredoc_is_still_flagged(tmp_path):
    """Positive control: the same line, not inside a heredoc, must still fire --
    otherwise a tracker that eats the whole file would pass the negative test above
    for the wrong reason."""
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\n.\n"})
    reviews = _check(root).reviews
    assert any("dot word" in r for r in reviews), reviews


# -- _check_typed_heredoc itself: self-referential case (#980) -- the function
# that *detects* heredoc openers used the bare comment test with no heredoc
# tracking of its own, so a heredoc body line that happens to contain a second
# "<<WORD"-shaped substring was read as a second, independent heredoc opener.

def test_heredoc_body_containing_double_angle_text_is_not_a_second_typed_heredoc(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\ncat << EOF\nfoo << BAR\nEOF\n"})
    offenders = _check(root).offenders
    heredoc_offenders = [o for o in offenders if "run.sh" in o and "<" "<" in o]
    assert len(heredoc_offenders) == 1, heredoc_offenders


# -- check_tree's own per-file eval/download REVIEW loop (unscoped to
# hooks/hooks.d/scripts -- every text file) --

def test_heredoc_body_eval_pattern_is_not_flagged_as_review(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\ncat << EOF\neval $(echo hi)\nEOF\n"})
    reviews = _check(root).reviews
    assert not any("eval fed by" in r for r in reviews), reviews


def test_eval_pattern_outside_heredoc_is_still_flagged_as_review(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\neval $(echo hi)\n"})
    reviews = _check(root).reviews
    assert any("eval fed by" in r for r in reviews), reviews


# -- _SCRIPT_DIRS-scoped, not-.sh-restricted checkers (_check_pwd_literal and
# its siblings _check_computed_command_word, _check_bare_env_word,
# _check_nested_default_expansion share this exact shape) --

def test_heredoc_body_pwd_literal_is_not_flagged(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\ncat << EOF\n$PWD\nEOF\n"})
    offenders = _check(root).offenders
    assert not any("PWD" in o for o in offenders), offenders


def test_pwd_literal_outside_heredoc_is_still_flagged(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\necho $PWD\n"})
    offenders = _check(root).offenders
    assert any("PWD" in o for o in offenders), offenders
