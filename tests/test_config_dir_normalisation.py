"""CLAUDE_CONFIG_DIR must be normalised the way PROJECT_DIR is (#169).

`claude_projects_dir()` concatenated `/projects` onto whatever the environment
held. On Windows that value usually arrives in native form
(`C:\\Users\\x\\.claude-alt`), producing a mixed-separator path, while
PROJECT_DIR is deliberately converted before it is slugged.

Whether MSYS resolved that anyway was never established — #169 was filed
unverified because there was no Windows machine to try it on, and the one test
that would have exercised it is `skipif(win32)` in line with the repo's
convention for bash-subprocess tests. So the path had **zero** coverage on the
only platform where it can fail.

Two answers to that here. The bash branch runs on POSIX CI against a stub
`cygpath` that mimics the MSYS conversion — the branch is Windows-only, the
logic is not. And the Python half runs unskipped on the Windows runner, because
it needs no bash at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SLUG_SH = REPO_ROOT / "scripts" / "lib-slug.sh"

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="bash subprocess assertions (#79)"
)

# What `cygpath -u` does to the forms that actually turn up.
CYGPATH_STUB = r"""#!/usr/bin/env bash
# Minimal stand-in for MSYS cygpath: -u converts a native Windows path to the
# POSIX form Git Bash uses. Enough for the one call claude_projects_dir makes.
if [ "$1" = "-u" ]; then
    p="$2"
    p="${p//\\//}"                    # backslashes to forward slashes
    case "$p" in
        [A-Za-z]:/*)
            drive="${p%%:*}"
            rest="${p#*:}"
            # Lowercase the drive letter without bash 4 case-folding, which
            # macOS's bash 3.2 does not have.
            drive=$(printf '%s' "$drive" | tr 'A-Z' 'a-z')
            p="/${drive}${rest}"
            ;;
    esac
    printf '%s\n' "$p"
    exit 0
fi
printf '%s\n' "$2"
"""


def _projects_dir(config_dir: str, *, with_cygpath: bool, tmp_path: Path) -> str:
    """Run claude_projects_dir with CLAUDE_CONFIG_DIR set, optionally with a
    cygpath stub first on PATH."""
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir

    if with_cygpath:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "cygpath"
        stub.write_text(CYGPATH_STUB, encoding="utf-8")
        stub.chmod(0o755)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        ["bash", "-c", f'source "{LIB_SLUG_SH}"; claude_projects_dir'],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@posix_only
def test_a_native_windows_config_dir_is_converted(tmp_path):
    """The case #169 is about: no mixed separators reach the path."""
    out = _projects_dir(r"C:\Users\dev\.claude-alt", with_cygpath=True, tmp_path=tmp_path)

    assert "\\" not in out, f"a backslash survived into the projects path: {out!r}"
    assert out == "/c/Users/dev/.claude-alt/projects", out


@posix_only
def test_a_trailing_separator_does_not_double_up(tmp_path):
    """`C:\\Users\\dev\\.claude-alt\\` must not become `…//projects`."""
    out = _projects_dir(r"C:\Users\dev\.claude-alt\\", with_cygpath=True, tmp_path=tmp_path)
    assert "//projects" not in out, out
    assert out == "/c/Users/dev/.claude-alt/projects", out


@posix_only
@pytest.mark.parametrize("raw", ["/home/u/.claude", "/home/u/.claude/", "/home/u/.claude///"])
def test_posix_paths_are_unchanged_apart_from_trailing_slashes(raw, tmp_path):
    """No cygpath on PATH — the common case must be exactly as it was."""
    out = _projects_dir(raw, with_cygpath=False, tmp_path=tmp_path)
    assert out == "/home/u/.claude/projects", out


@posix_only
def test_the_default_is_still_home_dot_claude(tmp_path):
    """With CLAUDE_CONFIG_DIR unset, nothing about the default changes."""
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env["HOME"] = "/home/somebody"
    result = subprocess.run(
        ["bash", "-c", f'source "{LIB_SLUG_SH}"; claude_projects_dir'],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/home/somebody/.claude/projects"


@posix_only
@pytest.mark.parametrize("raw,expected", [
    ("/", "/projects"),
    ("///", "/projects"),
    ("\\\\", "\\\\/projects"),
])
def test_a_path_of_nothing_but_separators_is_not_promoted_to_the_root(raw, expected, tmp_path):
    """The stripping loop needs a floor.

    Without one, `/` and `///` strip to nothing and `/projects` lands at the
    filesystem root — and a lone `\\`, which is a RELATIVE path, becomes an
    absolute one pointing somewhere else entirely. Silently reinterpreting a
    path is the shape of every bug this file has had.
    """
    assert _projects_dir(raw, with_cygpath=False, tmp_path=tmp_path) == expected


@posix_only
def test_the_bash_and_python_builders_agree(tmp_path):
    """Two implementations build this path — they must name the same directory.

    `pipeline/extract.py` builds it in Python for the extractor; `lib-slug.sh`
    builds it in bash for the hooks. Unifying them the way the slug was unified
    is not available here: each consumer needs the path in the form its own
    runtime can open, and on Git Bash those forms genuinely differ (POSIX for
    bash, native for a Windows Python). What must never differ is which
    directory they mean.

    Where CI runs there is no cygpath, so the two forms coincide and can be
    compared directly. The Windows case is tracked in #194.
    """
    from pipeline.extract import _session_dir

    config_dir = str(tmp_path / "cfg")
    monkeypatched = os.environ.copy()
    monkeypatched["CLAUDE_CONFIG_DIR"] = config_dir

    bash_side = _projects_dir(config_dir, with_cygpath=False, tmp_path=tmp_path)

    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = config_dir
    try:
        python_side = _session_dir("/p")
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old

    assert python_side.startswith(bash_side + "/"), (
        f"the two builders disagree about the projects directory:\\n"
        f"  bash:   {bash_side}\\n  python: {python_side}"
    )


# ── The Python half, which needs no bash and therefore runs on Windows ───────

def _windows_bash() -> str | None:
    """Git Bash on a Windows runner, or None. NOT the WSL launcher in System32,
    which CreateProcess finds first on PATH."""
    if sys.platform != "win32":
        return "bash"
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(var)
        if not base:
            continue
        for rel in ("Git/bin/bash.exe", "Git/usr/bin/bash.exe"):
            cand = Path(base) / rel
            if cand.is_file():
                return str(cand)
    return None


def test_both_builders_reach_the_same_directory(tmp_path):
    """#194: the two builders produce different STRINGS on Windows.

    `lib-slug.sh` hands bash a POSIX path; `extract.py` hands Python a native
    one. Each consumer needs the form its own runtime can open, so identical
    strings is the wrong invariant — the right one is that both name the same
    directory. Nobody had established that, which is exactly the "MSYS probably
    handles it" reasoning #169 was filed about.

    So: create the directory from the Python side, then ask bash — through its
    own builder — whether it is there. On Windows that is a real answer rather
    than an argument; on POSIX it is the same assertion, cheaply.
    """
    bash = _windows_bash()
    if bash is None:
        pytest.skip("no Git Bash on this Windows runner")

    from pipeline.extract import _session_dir

    config_dir = tmp_path / "cfg"
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        python_side = Path(_session_dir(str(tmp_path / "proj")))
        python_side.mkdir(parents=True)
        (python_side / "marker.jsonl").write_text("{}", encoding="utf-8")

        # The bash builder gets only CLAUDE_CONFIG_DIR — it computes the rest.
        script = (
            f'source "{LIB_SLUG_SH}"; '
            'd="$(claude_projects_dir)/$(session_dir_slug "$1")"; '
            '[ -f "$d/marker.jsonl" ] && echo FOUND || echo "MISSING: $d"'
        )
        result = subprocess.run(
            [bash, "-c", script, "bash", str(tmp_path / "proj")],
            env={**os.environ, "PIPELINE_DIR": str(REPO_ROOT)},
            capture_output=True, text=True,
        )
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old

    assert "FOUND" in result.stdout, (
        "the shell could not find the transcript directory the Python side "
        f"created — the two builders do not name the same place:\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.parametrize("config_dir", [
    r"C:\Users\dev\.claude-alt",
    "C:/Users/dev/.claude-alt",
    r"C:\Users\dev\.claude-alt\\",
    "C:/Users/dev/.claude-alt/",
])
def test_session_dir_tolerates_every_windows_config_dir_form(config_dir, monkeypatch):
    """`_session_dir` builds the same projects path from any of the forms a
    Windows user's environment can hold.

    This one is deliberately NOT skipped on win32: #169's whole complaint is
    that the only test covering this path skips on the platform where it
    matters, so the half that can run there does.
    """
    from pipeline.extract import _session_dir

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", config_dir)
    out = _session_dir("/p")

    assert "//projects" not in out.replace("\\", "/"), out
    assert out.replace("\\", "/").endswith("/.claude-alt/projects/-p"), out
