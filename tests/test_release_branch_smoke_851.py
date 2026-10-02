"""#851 -- the hook smoke test run on a built release tree, and the workflow that
publishes it.

`.github/scripts/smoke_release_tree.py` runs every command hooks/hooks.json names,
once, with a minimal JSON payload on stdin, against a COPY of the tree, with HOME,
CLAUDE_PROJECT_DIR, TMPDIR and CLAUDE_PLUGIN_ROOT all inside one temp directory and a
fake `claude` first on PATH. Anything a hook leaves running is waited for, then killed.

The fixtures here use tiny stand-in hooks so the harness itself is tested: a harness
that never ran anything would pass "the failing hook is reported" only if that test
did not also exist next to "the passing hook ran and saw its payload".
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "smoke_release_tree.py"
BUILD = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-branch.yml"

posix_only = pytest.mark.skipif(os.name == "nt", reason="hook commands are bash; ps scan is POSIX")


def _load(path: Path, name: str):
    assert path.exists(), f"{path} does not exist (#851)"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, scripts: dict, events: dict | None = None) -> Path:
    root = tmp_path / "tree"
    (root / "hooks").mkdir(parents=True)
    (root / "scripts").mkdir()
    events = events or {name: name for name in scripts}
    hooks = {"hooks": {
        event: [{"hooks": [{"type": "command",
                            "command": f'bash "${{CLAUDE_PLUGIN_ROOT}}/scripts/{script}.sh"'}]}]
        for event, script in events.items()
    }}
    (root / "hooks" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    for name, body in scripts.items():
        (root / "scripts" / f"{name}.sh").write_text(body, encoding="utf-8")
    return root


RECORD = """#!/bin/bash
cat > "$CLAUDE_PROJECT_DIR/seen-$1${EVENT_TAG:-}.json"
printf '%s\\n' "$HOME" > "$CLAUDE_PROJECT_DIR/home.txt"
printf '%s\\n' "$CLAUDE_PLUGIN_ROOT" > "$CLAUDE_PROJECT_DIR/root.txt"
exit 0
"""


@posix_only
def test_every_hook_runs_once_with_its_event_payload(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    rec = RECORD.replace("$1${EVENT_TAG:-}", "$(basename \"$0\" .sh)")
    tree = _tree(tmp_path, {"SessionStart": rec, "UserPromptSubmit": rec,
                            "PostToolUse": rec, "SessionEnd": rec})
    result = mod.run_smoke(tree, validate="skip", linger_seconds=5, keep_dir=tmp_path / "keep")
    assert result.ok, result.report()
    assert [h.event for h in result.hooks] == ["SessionStart", "UserPromptSubmit",
                                               "PostToolUse", "SessionEnd"]
    assert all(h.returncode == 0 for h in result.hooks)
    project = tmp_path / "keep" / "project"
    for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "SessionEnd"):
        payload = json.loads((project / f"seen-{event}.json").read_text(encoding="utf-8"))
        assert payload["hook_event_name"] == event
        for key in ("session_id", "transcript_path", "cwd"):
            assert payload[key], key
        assert Path(payload["transcript_path"]).is_file()
    home = (project / "home.txt").read_text(encoding="utf-8").strip()
    assert home != os.path.expanduser("~")
    assert home.startswith(str(tmp_path / "keep"))
    root = (project / "root.txt").read_text(encoding="utf-8").strip()
    assert root != str(tree), "hooks must run against a copy, never the tree that ships"


@posix_only
def test_a_failing_hook_fails_the_smoke_and_is_named(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    tree = _tree(tmp_path, {"SessionStart": "exit 0\n", "SessionEnd": "echo boom >&2; exit 3\n"})
    result = mod.run_smoke(tree, validate="skip", linger_seconds=5)
    assert not result.ok
    bad = [h for h in result.hooks if h.returncode != 0]
    assert [h.event for h in bad] == ["SessionEnd"]
    assert "boom" in result.report()
    # Positive control: the passing hook in the same run is reported as passing.
    assert [h.returncode for h in result.hooks if h.event == "SessionStart"] == [0]


@posix_only
def test_a_hook_writing_into_its_plugin_root_does_not_touch_the_shipped_tree(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    tree = _tree(tmp_path, {"SessionStart": 'mkdir -p "$CLAUDE_PLUGIN_ROOT/__pycache__"; '
                                            'echo x > "$CLAUDE_PLUGIN_ROOT/__pycache__/a.pyc"\n'})
    before = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*"))
    result = mod.run_smoke(tree, validate="skip", linger_seconds=5)
    assert result.ok, result.report()
    assert sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")) == before


def _alive(marker: str) -> bool:
    out = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True,
                         check=False).stdout
    return any(marker in line for line in out.splitlines())


@posix_only
def test_a_lingering_background_child_is_killed_and_reported(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    marker = f"smoke-linger-{os.getpid()}-{time.time_ns()}"
    body = (f'nohup bash -c \'exec -a {marker} sleep 300\' >/dev/null 2>&1 &\n'
            "disown 2>/dev/null || true\nexit 0\n")
    tree = _tree(tmp_path, {"SessionStart": body})
    result = mod.run_smoke(tree, validate="skip", linger_seconds=1)
    assert result.killed, result.report()
    assert "killed" in result.report()
    time.sleep(0.5)
    assert not _alive(marker), "a background child outlived the smoke run"


@posix_only
def test_a_quick_background_child_is_waited_for_not_killed(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    body = ('( sleep 1; echo done > "$CLAUDE_PROJECT_DIR/bg-done" ) >/dev/null 2>&1 &\n'
            "exit 0\n")
    tree = _tree(tmp_path, {"SessionStart": body})
    result = mod.run_smoke(tree, validate="skip", linger_seconds=20, keep_dir=tmp_path / "keep")
    assert result.ok and not result.killed, result.report()
    assert (tmp_path / "keep" / "project" / "bg-done").exists()


@posix_only
def test_validate_required_without_a_claude_binary_fails_loudly(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    tree = _tree(tmp_path, {"SessionStart": "exit 0\n"})
    result = mod.run_smoke(tree, validate="require", claude_bin=str(tmp_path / "no-claude"),
                           linger_seconds=1)
    assert not result.ok
    assert "claude" in result.report() and "not found" in result.report()


@posix_only
def test_validate_skipped_says_so_out_loud(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    tree = _tree(tmp_path, {"SessionStart": "exit 0\n"})
    result = mod.run_smoke(tree, validate="skip", linger_seconds=1)
    assert result.ok
    assert "SKIPPED" in result.report() and "validate" in result.report()


# -- hooks (#858) -----------------------------------------------------------------
#
# A hookless tree, or a hooks.json declaring zero `command` hooks, must fail the
# smoke test loudly -- not print "nothing to run" and exit 0, which is what this
# plugin's own hooks.json-less install would otherwise do unnoticed.

def test_no_hooks_json_fails_the_smoke_and_is_named(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    tree = _tree(tmp_path, {"SessionStart": "exit 0\n"})
    (tree / "hooks" / "hooks.json").unlink()
    result = mod.run_smoke(tree, validate="skip", linger_seconds=1)
    assert not result.ok
    assert "hooks/hooks.json" in result.report()


@posix_only
def test_hooks_json_with_zero_command_hooks_fails_the_smoke(tmp_path):
    mod = _load(SCRIPT, "smoke_release_tree")
    tree = _tree(tmp_path, {"SessionStart": "exit 0\n"})
    (tree / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    result = mod.run_smoke(tree, validate="skip", linger_seconds=1)
    assert not result.ok
    assert "zero" in result.report() and "command hooks" in result.report()
    # Positive control: the same shape of tree, with its original one-hook
    # hooks.json intact, passes.
    passing = _tree(tmp_path / "passing", {"SessionStart": "exit 0\n"})
    assert mod.run_smoke(passing, validate="skip", linger_seconds=1).ok


@posix_only
def test_this_repository_built_tree_passes_the_hook_smoke(tmp_path):
    """Integration: the real hooks from a real build, in isolation."""
    build = _load(BUILD, "build_release_tree")
    out = tmp_path / "out"
    build.build(REPO_ROOT, "HEAD", out, build.load_config(REPO_ROOT / ".github" / "release-branch.json"))
    mod = _load(SCRIPT, "smoke_release_tree")
    result = mod.run_smoke(out, validate="skip", linger_seconds=30)
    assert result.ok, result.report()
    assert {h.event for h in result.hooks} == {"SessionStart", "UserPromptSubmit",
                                               "PostToolUse", "SessionEnd"}


# -- the workflow ---------------------------------------------------------------

SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def _workflow() -> dict:
    assert WORKFLOW.exists(), f"{WORKFLOW} does not exist (#851)"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_triggers_on_version_tags_and_manual_dispatch_with_a_ref():
    doc = _workflow()
    on = doc.get(True, doc.get("on"))
    assert on["push"]["tags"] == ["v*"]
    assert "branches" not in on["push"], "a push to main must not publish the release branch"
    assert "ref" in on["workflow_dispatch"]["inputs"]


def test_workflow_permissions_are_read_only_except_the_publishing_job():
    doc = _workflow()
    assert doc["permissions"] == {"contents": "read"}
    writers = [name for name, job in doc["jobs"].items()
               if (job.get("permissions") or {}).get("contents") == "write"]
    assert len(writers) == 1, writers
    for job in doc["jobs"].values():
        assert set((job.get("permissions") or {}).keys()) <= {"contents"}


def test_workflow_actions_are_pinned_to_a_commit_sha():
    doc = _workflow()
    uses = [s["uses"] for j in doc["jobs"].values() for s in j["steps"] if "uses" in s]
    assert uses
    for u in uses:
        assert SHA_PINNED.match(u), u


def test_workflow_pushes_only_after_build_check_and_smoke():
    doc = _workflow()
    text = WORKFLOW.read_text(encoding="utf-8")
    steps = [s for j in doc["jobs"].values() for s in j["steps"]]
    runs = [s.get("run", "") for s in steps]
    idx = {key: next(i for i, r in enumerate(runs) if key in r)
           for key in ("build_release_tree.py", "check_release_tree.py",
                       "smoke_release_tree.py", "git push")}
    assert idx["build_release_tree.py"] < idx["check_release_tree.py"] \
        < idx["smoke_release_tree.py"] < idx["git push"]
    assert "refs/heads/release" in text
    # Pushed on top of the previous release commit, never forced: a catalogue that
    # pinned an older release sha must still be able to fetch it.
    assert "git push origin" in text
    assert "--force origin" not in text and "push -f" not in text
    assert '-p "$parent"' in text
    # No step may be allowed to fail open on the way to the push.
    for s in steps:
        assert not s.get("continue-on-error"), s.get("name")


# -- #856: three follow-ups from the #853 review --------------------------------

def test_workflow_pins_pyyaml():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pip install --disable-pip-version-check pyyaml" not in text, \
        "pyyaml must be pinned, not installed unpinned"
    assert 'pyyaml==${PYYAML_VERSION}' in text
    doc = _workflow()
    assert re.match(r"^\d+\.\d+(\.\d+)?$", str(doc["env"]["PYYAML_VERSION"]))


def test_workflow_refuses_to_publish_a_version_that_does_not_move_release_forward():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "check_version_order.py" in text
    idx_check = text.index("check_version_order.py")
    # The actual push command, not the unrelated mention in the top-of-file
    # comment about who must push the tag.
    idx_push = text.rindex('git push origin "$commit')
    assert idx_check < idx_push, "the version-order guard must run before the push"
    # The guard has an explicit, named override -- not a silent skip.
    doc = _workflow()
    assert "allow_version_regression" in doc[True]["workflow_dispatch"]["inputs"]


def test_workflow_concurrency_comment_does_not_claim_a_force_push_race():
    """#856 item 3: the push is a plain push (test above pins this), so the
    comment above `concurrency:` must not still describe a force-push race."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "racing a force-push" not in text
    assert "never forced" in text or "plain (never forced)" in text
