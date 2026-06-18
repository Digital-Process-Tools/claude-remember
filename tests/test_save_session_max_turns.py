"""Guard the nested Haiku `claude -p` max-turns against Claude Code 2.x turn counting.

Regression context (#98, #100):
  - CC 1.x: `--max-turns 1` = one full prompt→reply cycle.
  - CC 2.1.x: turn 1 = prompt delivery, turn 2 = model reply. So `--max-turns 1`
    now exits 1 with subtype `error_max_turns` *before* the model replies, and
    save-session.sh aborts at the `exit 1` guard — no memory is ever written.
  - #100: a user Stop hook consumes a further turn, so even 2 can be too tight;
    the value must be configurable (REMEMBER_MAX_TURNS) with margin.

Both `claude -p` call sites (the main Haiku call and the NDC compression call)
must use the same configurable, >=2 value — never a hardcoded `--max-turns 1`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "save-session.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


def test_no_hardcoded_max_turns_1(script_text: str) -> None:
    """The literal `--max-turns 1` is the bug — it must not appear anywhere."""
    offenders = [
        ln
        for ln in script_text.splitlines()
        if re.search(r"--max-turns\s+1\b", ln)
    ]
    assert not offenders, f"hardcoded `--max-turns 1` (breaks on CC 2.x): {offenders}"


def test_both_claude_calls_use_configurable_max_turns(script_text: str) -> None:
    """Every `claude -p` invocation must pass --max-turns "$MAX_TURNS"."""
    flags = re.findall(r"--max-turns\s+(\S+)", script_text)
    claude_calls = script_text.count("claude -p")
    assert claude_calls >= 2, f"expected >=2 `claude -p` call sites, found {claude_calls}"
    assert len(flags) >= 2, f"expected >=2 --max-turns flags, found {flags}"
    for value in flags:
        assert "MAX_TURNS" in value, f"--max-turns must reference $MAX_TURNS, got {value!r}"


def test_max_turns_defaults_to_at_least_2(script_text: str) -> None:
    """REMEMBER_MAX_TURNS default must clear the CC 2.x prompt-delivery turn (>=2)."""
    m = re.search(r'MAX_TURNS="?\$\{REMEMBER_MAX_TURNS:-(\d+)\}"?', script_text)
    assert m, "expected `MAX_TURNS=\"${REMEMBER_MAX_TURNS:-N}\"` assignment"
    assert int(m.group(1)) >= 2, f"default {m.group(1)} too low for CC 2.x turn counting"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_env_override_flows_through() -> None:
    """REMEMBER_MAX_TURNS env override is honored at runtime; default applies otherwise."""
    assign = next(
        ln.strip()
        for ln in SCRIPT.read_text().splitlines()
        if ln.strip().startswith("MAX_TURNS=")
    )
    # Default (unset)
    out = subprocess.run(
        ["bash", "-c", f'unset REMEMBER_MAX_TURNS; {assign}; echo "$MAX_TURNS"'],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert int(out) >= 2
    # Override
    out = subprocess.run(
        ["bash", "-c", f'export REMEMBER_MAX_TURNS=7; {assign}; echo "$MAX_TURNS"'],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "7"
