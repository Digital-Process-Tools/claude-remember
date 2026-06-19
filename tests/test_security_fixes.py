"""Security regression tests for issue #66.

Covers:
  1. safe_eval no longer executes injected shell commands (RCE fix)
  2. Trap path cleanup works when TMPDIR contains a space
  3. _jq_fallback handles a file path containing a single quote
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"
DETECT_TOOLS_SH = REPO_ROOT / "scripts" / "detect-tools.sh"
LIB_MEMORY_DIR_SH = REPO_ROOT / "scripts" / "lib-memory-dir.sh"
RESOLVE_PATHS_SH = REPO_ROOT / "scripts" / "resolve-paths.sh"


def _bash_path(p) -> str:
    """Forward-slash drive form, usable by BOTH Git Bash and the Windows
    ``python3`` that the bash scripts invoke (jq fallback, migration paths).

    `C:\\Users\\x` -> `C:/Users/x`. Git Bash and Windows Python both accept
    forward-slash drive paths; the MSYS `/c/x` form works in bash but Windows
    Python can't ``open()`` it. On POSIX the path is returned unchanged.
    """
    return str(p).replace("\\", "/")


def _find_bash():
    """Return the bash executable to use for subprocess calls.

    On POSIX, plain "bash". On Windows, the Git-for-Windows bash (NOT the
    System32 WSL launcher, which CreateProcess finds first on PATH). Returns
    None on Windows when Git Bash isn't installed → the module is skipped.
    """
    if sys.platform != "win32":
        return "bash"
    import shutil
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


_BASH = _find_bash()

pytestmark = pytest.mark.skipif(_BASH is None, reason="Git Bash not found (Windows without Git for Windows)")


# ---------------------------------------------------------------------------
# 1. safe_eval — RCE injection must NOT execute embedded commands
# ---------------------------------------------------------------------------

def test_safe_eval_rejects_rce_semicolon_injection(tmp_path):
    """EXTRACT_FILE=/tmp/x; rm -rf canary must NOT execute rm."""
    canary = tmp_path / "canary.txt"
    canary.write_text("should not be deleted")

    script = f"""
set +e
source "{_bash_path(LOG_SH)}"
safe_val << 'EVAL_INPUT'
EXTRACT_FILE={canary}; rm -f {canary}
EVAL_INPUT
""".replace("safe_val", "safe_eval")
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PROJECT_DIR": _bash_path(REPO_ROOT)},
    )
    assert canary.exists(), (
        f"safe_eval executed injected 'rm -f {canary}' — RCE is still present. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_safe_eval_rejects_rce_command_substitution(tmp_path):
    """EXTRACT_FILE=$(rm -f canary) must NOT execute the substitution."""
    canary = tmp_path / "canary2.txt"
    canary.write_text("should not be deleted")

    script = f"""
set +e
source "{_bash_path(LOG_SH)}"
safe_val << 'EVAL_INPUT'
EXTRACT_FILE=$(rm -f {canary})
EVAL_INPUT
""".replace("safe_val", "safe_eval")
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PROJECT_DIR": _bash_path(REPO_ROOT)},
    )
    assert canary.exists(), (
        f"safe_eval executed command substitution in value — RCE present. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_safe_eval_assigns_normal_variables():
    """Legitimate KEY=value lines must still be assigned."""
    script = f"""
set -e
source "{_bash_path(LOG_SH)}"
safe_val << 'EVAL_INPUT'
EXTRACT_FILE=/tmp/legit-path.txt
EXCHANGE_COUNT=42
HUMAN_COUNT=7
EVAL_INPUT
echo "EXTRACT_FILE=$EXTRACT_FILE"
echo "EXCHANGE_COUNT=$EXCHANGE_COUNT"
echo "HUMAN_COUNT=$HUMAN_COUNT"
""".replace("safe_val", "safe_eval")
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PROJECT_DIR": _bash_path(REPO_ROOT)},
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert "EXTRACT_FILE=/tmp/legit-path.txt" in result.stdout
    assert "EXCHANGE_COUNT=42" in result.stdout
    assert "HUMAN_COUNT=7" in result.stdout


def test_safe_eval_assigns_value_with_equals_sign():
    """Values containing '=' (e.g. base64) must be stored literally."""
    script = f"""
set -e
source "{_bash_path(LOG_SH)}"
safe_val << 'EVAL_INPUT'
EXTRACT_FILE=/tmp/a=b=c
EVAL_INPUT
echo "RESULT=$EXTRACT_FILE"
""".replace("safe_val", "safe_eval")
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PROJECT_DIR": _bash_path(REPO_ROOT)},
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert "RESULT=/tmp/a=b=c" in result.stdout


def test_safe_eval_ignores_lowercase_keys():
    """Lowercase variable names must be silently ignored (not assigned)."""
    script = f"""
set +e
source "{_bash_path(LOG_SH)}"
safe_val << 'EVAL_INPUT'
lowercase_var=something
EVAL_INPUT
echo "lowercase_var=${{lowercase_var:-UNSET}}"
""".replace("safe_val", "safe_eval")
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PROJECT_DIR": _bash_path(REPO_ROOT)},
    )
    assert "lowercase_var=UNSET" in result.stdout, (
        f"lowercase key was assigned: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# 2. Trap cleanup — TMPDIR with a space must not delete wrong paths
# ---------------------------------------------------------------------------

def test_trap_cleanup_with_space_in_tmpdir(tmp_path):
    """EXIT trap must clean up the correct tmp file when TMPDIR has a space."""
    spaced_tmp = tmp_path / "my dir"
    spaced_tmp.mkdir()

    project = tmp_path / "proj"
    (project / ".claude" / "remember").mkdir(parents=True)
    (project / ".remember").mkdir(parents=True)

    script = f"""
set -e
export CLAUDE_PROJECT_DIR="{_bash_path(project)}"
export PIPELINE_DIR="{_bash_path(REPO_ROOT)}"
source "{_bash_path(RESOLVE_PATHS_SH)}"
export TMPDIR="{_bash_path(spaced_tmp)}"
source "{_bash_path(LIB_MEMORY_DIR_SH)}"
echo "CONFIG=$REMEMBER_CONFIG"
"""
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": _bash_path(tmp_path)},
    )
    assert result.returncode == 0, (
        f"lib-memory-dir.sh failed with spaced TMPDIR: {result.stderr}"
    )

    cfg_path = None
    for line in result.stdout.splitlines():
        if line.startswith("CONFIG="):
            cfg_path = line[len("CONFIG="):]
    assert cfg_path is not None, f"CONFIG not found in output: {result.stdout!r}"
    assert not Path(cfg_path).exists(), (
        f"EXIT trap did not clean up {cfg_path} — path-with-space bug may be present"
    )

    stray = list(spaced_tmp.iterdir())
    assert len(stray) == 0, (
        f"Stray files in spaced TMPDIR after cleanup: {stray}"
    )



JQ_TEST_HELPER = REPO_ROOT / "tests" / "fixtures" / "jq_fallback_test_helper.sh"


def test_jq_fallback_path_with_single_quote(tmp_path):
    """_jq_fallback must work when the JSON file path contains a single quote."""
    quoted_dir = tmp_path / "it's a dir"
    quoted_dir.mkdir()
    json_file = quoted_dir / "config.json"
    json_file.write_text(json.dumps({"thresholds": {"min_human_messages": 3}}))

    script = f"""
set -e
source "{_bash_path(JQ_TEST_HELPER)}"
result=$(_jq_fallback -r '.thresholds.min_human_messages' "{_bash_path(json_file)}")
echo "RESULT=$result"
"""
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"_jq_fallback failed on path with single quote: {result.stderr}"
    )
    assert "RESULT=3" in result.stdout, (
        f"Unexpected output from _jq_fallback: {result.stdout!r}"
    )


def test_jq_fallback_path_with_spaces(tmp_path):
    """_jq_fallback must work when the file path contains spaces."""
    spaced_dir = tmp_path / "config dir"
    spaced_dir.mkdir()
    json_file = spaced_dir / "config.json"
    json_file.write_text(json.dumps({"key": "hello world"}))

    script = f"""
set -e
source "{_bash_path(JQ_TEST_HELPER)}"
result=$(_jq_fallback -r '.key' "{_bash_path(json_file)}")
echo "RESULT=$result"
"""
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"_jq_fallback failed on path with spaces: {result.stderr}"
    )
    assert "RESULT=hello world" in result.stdout, (
        f"Unexpected output from _jq_fallback: {result.stdout!r}"
    )


def test_jq_fallback_nested_key(tmp_path):
    """_jq_fallback must resolve multi-level dot-notation keys."""
    json_file = tmp_path / "config.json"
    json_file.write_text(json.dumps({"a": {"b": {"c": 42}}}))

    script = f"""
set -e
source "{_bash_path(JQ_TEST_HELPER)}"
result=$(_jq_fallback -r '.a.b.c' "{_bash_path(json_file)}")
echo "RESULT=$result"
"""
    result = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    assert "RESULT=42" in result.stdout, f"Unexpected: {result.stdout!r}"
