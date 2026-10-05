"""#905: FAIL on a shell function whose whole body is a single call to
another function with a positional parameter ($1..$9, ${1}..) spliced into
a string argument.

Observed in the directory portal (2026-10-05, claude-directory-publishing
triggers.md 14): #899 folded scripts/log.sh's three dispatch reporters
(_dispatch_report_failure/_skip/_timeout) into one-liners --

    _dispatch_report_failure() {
        report_error "dispatch" "ERROR: hook failed: $1/$2 (exit $3): $4"
    }

-- and the portal held exactly the three hooks that compile log.sh's
dispatch in, with COMMAND_SCRIPT_NOT_FOLLOWED. r40 restored each reporter to
a self-contained body (own locals, own #618 flatten, own two writes) and the
hold cleared. tests/test_dispatch_reporter_shape_898.py already pins this
shape for those three named functions; this needle generalizes it to any
shipped shell function, in check_release_tree.py itself, with a red test
first and a positive control on a self-contained reporter that must pass.

Every "must fail" assertion here is paired with a "must NOT fail" one, the
same convention as test_release_branch_check_900.py and
test_dispatch_reporter_shape_898.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"

KIB = 1024
BUDGET = {"max_file_bytes": 256 * KIB, "max_files": 512, "max_total_bytes": 3 * KIB * KIB}

NEEDLE_MARKER = "delegates its whole body to"


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#905)"
    spec = importlib.util.spec_from_file_location("check_release_tree_905", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree_905"] = mod
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
        "scripts/session-start-hook.sh": b"#!/bin/sh\necho hi\n",
        "hooks/hooks.json": json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/scripts/session-start-hook.sh"},
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


HELD_SCRIPT = (
    b"#!/bin/sh\n"
    b"_dispatch_report_failure() {\n"
    b'    report_error "dispatch" "ERROR: hook failed: $1/$2 (exit $3): $4"\n'
    b"}\n"
    b"echo hi\n"
)

SELF_CONTAINED_SCRIPT = (
    b"#!/bin/sh\n"
    b"_dispatch_report_failure() {\n"
    b'    local hook="$1" cmd="$2" code="$3" msg="$4"\n'
    b'    msg=$(printf "%s" "$msg" | tr \'[:cntrl:]\' \' \')\n'
    b'    printf \'%s\\n\' "$msg" >> hook-errors.log\n'
    b"}\n"
    b"echo hi\n"
)

BARE_PASSTHROUGH_SCRIPT = b'#!/bin/sh\n_wrap() {\n    other_func "$1"\n}\necho hi\n'

MULTI_STATEMENT_SCRIPT = (
    b'#!/bin/sh\n_wrap() {\n'
    b'    local x="$1"\n'
    b'    other_func "msg: $x"\n'
    b"}\necho hi\n"
)

# r38/r39's own actual shape was one physical line, not the multi-line
# pretty-printed form used above -- the needle must catch that too.
HELD_ONE_LINE_SCRIPT = (
    b'#!/bin/sh\n'
    b'_dispatch_report_failure() { report_error "dispatch" "ERROR: hook failed: $1/$2 (exit $3): $4"; }\n'
    b"echo hi\n"
)

# a shipped .sh checked out with CRLF line endings (no .gitattributes forces
# LF) must not blind the needle to the exact same held shape.
HELD_CRLF_SCRIPT = (
    b"#!/bin/sh\r\n"
    b"_dispatch_report_failure() {\r\n"
    b'    report_error "dispatch" "ERROR: hook failed: $1/$2 (exit $3): $4"\r\n'
    b"}\r\n"
    b"echo hi\r\n"
)


def test_a_one_line_delegate_with_positional_param_in_a_string_fails(tmp_path):
    root = _tree(tmp_path, {"scripts/lib-dispatch.sh": HELD_SCRIPT})
    offenders = _check(root).offenders
    assert any("lib-dispatch.sh" in o and NEEDLE_MARKER in o and "_dispatch_report_failure" in o
               for o in offenders), offenders


def test_a_self_contained_reporter_does_not_fail(tmp_path):
    # must-NOT-fire control: the r40 shape (own locals, own flatten, own
    # write) must not be flagged by this guard.
    root = _tree(tmp_path, {"scripts/lib-dispatch.sh": SELF_CONTAINED_SCRIPT})
    offenders = _check(root).offenders
    assert not any(NEEDLE_MARKER in o for o in offenders), offenders


def test_a_bare_positional_passthrough_does_not_fail(tmp_path):
    # "$1" alone (a named-local-style passthrough) is fine -- only a
    # positional parameter SPLICED INTO a longer string is the held shape.
    root = _tree(tmp_path, {"scripts/lib-pass.sh": BARE_PASSTHROUGH_SCRIPT})
    offenders = _check(root).offenders
    assert not any(NEEDLE_MARKER in o for o in offenders), offenders


def test_a_multi_statement_function_does_not_fail(tmp_path):
    # the needle only fires when the WHOLE body is a single delegating call.
    root = _tree(tmp_path, {"scripts/lib-multi.sh": MULTI_STATEMENT_SCRIPT})
    offenders = _check(root).offenders
    assert not any(NEEDLE_MARKER in o for o in offenders), offenders


def test_a_one_physical_line_delegate_fails(tmp_path):
    # r38/r39's actual on-disk shape -- the whole `name() { stmt; }` on one
    # line -- is at least as common as the pretty-printed multi-line form,
    # and the needle must catch it too.
    root = _tree(tmp_path, {"scripts/lib-dispatch-oneline.sh": HELD_ONE_LINE_SCRIPT})
    offenders = _check(root).offenders
    assert any("lib-dispatch-oneline.sh" in o and NEEDLE_MARKER in o
               and "_dispatch_report_failure" in o for o in offenders), offenders


def test_the_same_held_shape_under_crlf_line_endings_fails(tmp_path):
    # this repo ships no .gitattributes, so nothing forces LF on checkout --
    # a CRLF-encoded .sh must trip the same needle as its LF twin.
    root = _tree(tmp_path, {"scripts/lib-dispatch-crlf.sh": HELD_CRLF_SCRIPT})
    offenders = _check(root).offenders
    assert any("lib-dispatch-crlf.sh" in o and NEEDLE_MARKER in o
               and "_dispatch_report_failure" in o for o in offenders), offenders


def test_an_unquoted_positional_splice_fails(tmp_path):
    # the same held shape without the surrounding double quotes -- an
    # unquoted bareword argument with $1 embedded is just as unfollowable.
    root = _tree(tmp_path, {
        "scripts/lib-unquoted.sh": b"#!/bin/sh\n_wrap() {\n    other_func msg:$1\n}\necho hi\n",
    })
    offenders = _check(root).offenders
    assert any("lib-unquoted.sh" in o and NEEDLE_MARKER in o for o in offenders), offenders


def test_an_unquoted_bare_passthrough_does_not_fail(tmp_path):
    # must-NOT-fire control for the unquoted case: a bare $1 standing alone
    # as its own argument is a passthrough, not a splice.
    root = _tree(tmp_path, {
        "scripts/lib-unquoted-bare.sh": b"#!/bin/sh\n_wrap() {\n    other_func $1\n}\necho hi\n",
    })
    offenders = _check(root).offenders
    assert not any(NEEDLE_MARKER in o for o in offenders), offenders


def test_a_function_keyword_declared_delegate_fails(tmp_path):
    # `function name() { ... }` is as common as the bare `name() { ... }`
    # form and must trip the same needle.
    root = _tree(tmp_path, {
        "scripts/lib-function-kw.sh": (
            b'#!/bin/sh\nfunction _wrap() {\n'
            b'    report_error "dispatch" "ERROR: $1/$2"\n'
            b"}\necho hi\n"
        ),
    })
    offenders = _check(root).offenders
    assert any("lib-function-kw.sh" in o and NEEDLE_MARKER in o for o in offenders), offenders


def test_todays_log_sh_dispatch_reporters_do_not_trip_this_needle():
    # confirms today's tree (post-#899 r40) does not trip the new needle.
    assert LOG_SH.exists(), f"{LOG_SH} does not exist"
    root_files = {"scripts/log.sh": LOG_SH.read_bytes()}
    with tempfile.TemporaryDirectory() as td:
        root = _tree(Path(td), root_files)
        offenders = _check(root).offenders
    assert not any(NEEDLE_MARKER in o for o in offenders), offenders
