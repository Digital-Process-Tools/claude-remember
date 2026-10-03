"""#900: a hooks.json-registered hook that still sources another file FAILs
the release-tree check -- the shape build_release_tree.py's own compile step
(compile_hooks.py) exists to eliminate before this check ever sees the file.

Every "must fail" assertion here is paired with a "must pass" one, the same
convention as test_release_branch_check_851.py and _898.py.
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
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#900)"
    spec = importlib.util.spec_from_file_location("check_release_tree_900", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree_900"] = mod
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


def test_hook_script_names_is_imported_from_compile_hooks():
    mod = _load()
    import importlib.util as _ilu
    compile_spec = _ilu.spec_from_file_location(
        "compile_hooks_900", REPO_ROOT / ".github" / "scripts" / "compile_hooks.py")
    compile_mod = _ilu.module_from_spec(compile_spec)
    sys.modules["compile_hooks_900"] = compile_mod
    compile_spec.loader.exec_module(compile_mod)
    assert mod.HOOK_SCRIPT_NAMES == compile_mod.HOOK_SCRIPT_NAMES


def test_a_hooks_json_registered_hook_that_still_sources_a_file_fails(tmp_path):
    root = _tree(tmp_path, {
        "scripts/session-start-hook.sh":
            b'#!/bin/sh\nsource "${CLAUDE_PLUGIN_ROOT}/scripts/lib.sh"\necho hi\n',
        "scripts/lib.sh": b"#!/bin/sh\necho from-lib\n",
    })
    offenders = _check(root).offenders
    assert any("still sources another file" in o and "session-start-hook.sh" in o
               for o in offenders)


def test_a_hooks_json_registered_hook_with_no_source_line_does_not_fail(tmp_path):
    # the must-fire case above needs a must-NOT-fire control: a clean,
    # fully self-contained hook must not be flagged by this guard.
    root = _tree(tmp_path, {})
    offenders = _check(root).offenders
    assert not any("still sources another file" in o for o in offenders)


def test_a_non_hook_script_that_sources_a_file_is_not_flagged_by_this_guard(tmp_path):
    # scoped to the four hooks.json-registered names -- an ordinary script
    # (save-session.sh, doctor.sh, ...) sourcing a sibling is normal and out
    # of this guard's scope entirely.
    root = _tree(tmp_path, {
        "scripts/save-session.sh":
            b'#!/bin/sh\nsource "${CLAUDE_PLUGIN_ROOT}/scripts/lib.sh"\necho hi\n',
        "scripts/lib.sh": b"#!/bin/sh\necho from-lib\n",
    })
    offenders = _check(root).offenders
    assert not any("still sources another file" in o for o in offenders)


def test_a_dot_source_statement_is_caught_too(tmp_path):
    root = _tree(tmp_path, {
        "scripts/session-start-hook.sh":
            b'#!/bin/sh\n. "${CLAUDE_PLUGIN_ROOT}/scripts/lib.sh"\necho hi\n',
        "scripts/lib.sh": b"#!/bin/sh\necho from-lib\n",
    })
    offenders = _check(root).offenders
    assert any("still sources another file" in o and "session-start-hook.sh" in o
               for o in offenders)


def test_a_function_argument_literally_named_source_is_not_mistaken_for_a_source_statement(tmp_path):
    # the exact false-positive this repo's own session-start-hook.sh:334
    # would otherwise trip on.
    root = _tree(tmp_path, {
        "scripts/session-start-hook.sh":
            b'#!/bin/sh\n_stdin_json_string_into VAR source "$HOOK_STDIN"\necho hi\n',
    })
    offenders = _check(root).offenders
    assert not any("still sources another file" in o for o in offenders)
