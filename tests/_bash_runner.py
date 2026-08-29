"""Shared helper for locating a real POSIX bash to drive subprocess-based
hook tests across platforms (#432).

On macOS and Linux, ``bash`` on PATH already is a real bash. On Windows,
``bash`` on PATH commonly resolves to the WSL launcher, not Git Bash -- the
bash that actually ships alongside Git for Windows and that a Windows user
invoking git-based tooling (the #82 reporter's own setup) actually has.

``find_git_bash`` is lifted verbatim from tests/test_hooks_json.py, written
for #82. That module now imports it from here instead of defining its own
copy, so the two Windows-bash-detection tests in test_hook_cwd_leak_417.py
and test_transcript_path_leak_424.py stop being skipped for the whole
platform (#432) and instead run for real under Git Bash, exactly as
test_hooks_json.py's own Git-Bash tests already do.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def find_git_bash() -> str | None:
    """Locate Git Bash specifically (not WSL's bash launcher).

    On Windows, `shutil.which("bash")` may resolve to the WSL launcher. The
    #82 reporter launches Claude Code from Git Bash, so we want *that* bash.
    Probe the standard Git-for-Windows install locations; fall back to PATH
    only if the resolved binary lives under a Git install. Returns the exe
    path or None.
    """
    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    resolved = shutil.which("bash")
    if resolved and "git" in resolved.replace("\\", "/").lower():
        return resolved
    return None


def resolve_bash() -> str | None:
    """Return a real POSIX bash to invoke, or None if none is available.

    Non-Windows: `bash` on PATH already is a real bash, so PATH resolution
    is trusted as-is. Windows: PATH's `bash` is commonly the WSL launcher,
    not Git Bash, so probe for Git Bash specifically instead of trusting
    PATH.

    Callers that need a genuinely portable subprocess bash (rather than one
    tied to a specific test's platform assumptions) use this instead of the
    literal string "bash", so that a missing interpreter is reported as
    "not found" rather than silently running the wrong bash.
    """
    if sys.platform != "win32":
        return shutil.which("bash")
    return find_git_bash()
