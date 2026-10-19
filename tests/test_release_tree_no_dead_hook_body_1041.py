"""The release tree ships no hook whose body can never run.

The Anthropic directory's scanner reads code that never runs (triggers.md: "it reads
functions nobody calls"). `hooks.d/after_save/60-git-reconcile.sh` has been
hard-disabled since #969 -- an unconditional `exit 0` near the top -- yet every
release since v0.42.0 shipped its ~430 unreachable lines, which pair
credential-prompt environment names (`GIT_ASKPASS`, `SSH_ASKPASS`, `core.askPass`)
with a fetch and a push to a remote. That is the read-plus-send shape of the
directory's "Uses a credential from the user's machine" finding
(MCP_FORWARDS_CREDENTIAL_ENV), for code that does nothing at run time.

Every "must not ship" assertion here is paired with a "must ship" one: a build that
produced an empty tree would pass the first kind on its own.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"
RECONCILE = "hooks.d/after_save/60-git-reconcile.sh"


def _load():
    spec = importlib.util.spec_from_file_location("build_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


def _build(tmp_path: Path) -> Path:
    mod = _load()
    out = tmp_path / "out"
    mod.build(REPO_ROOT, "HEAD", out, mod.load_config(CONFIG))
    return out


def _dead_tail(text: str) -> int | None:
    """Line number of an unconditional top-level `exit` that has code after it.

    Top level means column 0: the shipped scripts indent every block body, so an
    `exit` written at column 0 is reached unconditionally. Returns None when the
    script has no such exit, or when nothing but blank lines follows it.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.fullmatch(r"exit(\s+\d+)?\s*", line) and any(
                rest.strip() for rest in lines[i + 1:]):
            return i + 1
    return None


def test_the_detector_finds_a_dead_tail_and_passes_a_live_script():
    # Positive control for the scan below: it must fire on the shape, and only on it.
    assert _dead_tail("#!/bin/bash\nx=1\nexit 0\n\necho unreachable\n") == 3
    assert _dead_tail("#!/bin/bash\nif a; then\n    exit 0\nfi\necho live\n") is None
    assert _dead_tail("#!/bin/bash\necho live\nexit 0\n\n") is None


def test_the_reconcile_hook_is_still_hard_disabled_on_main():
    # The deny entry is only right while the hook cannot run. When #969's real fix
    # re-enables it, this fails: drop the entry from release-branch.json so the
    # feature ships again.
    text = (REPO_ROOT / RECONCILE).read_text(encoding="utf-8")
    assert _dead_tail(text) is not None, (
        f"{RECONCILE} no longer exits unconditionally -- remove it from the "
        "release deny-list in .github/release-branch.json so it ships again")


def test_the_release_tree_does_not_ship_the_hard_disabled_reconcile_hook(tmp_path):
    out = _build(tmp_path)
    assert not (out / RECONCILE).exists(), (
        f"{RECONCILE} is hard-disabled (#969) but shipped: its unreachable body pairs "
        "credential-prompt names with a fetch and a push for the directory's scanner")
    # Positive controls: the live, opt-in backup and restore hooks still ship.
    assert (out / "hooks.d/after_save/50-git-backup.sh").is_file()
    assert (out / "hooks.d/before_session_start/50-git-restore.sh").is_file()


def test_no_shipped_shell_script_carries_a_dead_tail(tmp_path):
    out = _build(tmp_path)
    scripts = sorted(out.rglob("*.sh"))
    assert len(scripts) > 10, "the build shipped almost no shell -- the scan below proves nothing"
    offenders = []
    for path in scripts:
        line = _dead_tail(path.read_text(encoding="utf-8", errors="replace"))
        if line is not None:
            offenders.append(f"{path.relative_to(out)}:{line}")
    assert not offenders, (
        "shipped scripts with code after an unconditional top-level exit (the "
        f"directory's scanner reads that code anyway): {offenders}")
