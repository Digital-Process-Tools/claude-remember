"""save-session.sh must delegate the Haiku call to the single pipeline entry
point (`pipeline.shell call-haiku`) — never inline its own `claude -p`.

The inline duplication is exactly what let the CC-2.x `--max-turns 1` break
(#98/#100) get half-fixed and the MCP-sandbox flags drift (#94): two call sites,
two chances to diverge. The `claude` invocation now lives only in
pipeline/haiku.py; max-turns / mcp-flag behaviour is covered there
(tests/test_haiku.py).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "save-session.sh"


def _code_lines(text: str) -> list[str]:
    # Drop comment lines so prose that mentions `claude -p` doesn't trip checks.
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def test_no_inline_claude_invocation() -> None:
    """The script must not invoke `claude` directly — that lives in haiku.py."""
    offenders = [ln for ln in _code_lines(SCRIPT.read_text()) if "claude -p" in ln]
    assert not offenders, f"inline claude invocation in save-session.sh: {offenders}"


def test_both_haiku_calls_delegate_to_call_haiku() -> None:
    """Main Haiku call + NDC compression both go through `call-haiku`."""
    code = "\n".join(_code_lines(SCRIPT.read_text()))
    n = code.count("pipeline.shell call-haiku")
    assert n == 2, f"expected 2 call-haiku delegations (main + NDC), found {n}"


def test_no_hardcoded_max_turns_in_script() -> None:
    """No `--max-turns` literal survives in the shell — it's haiku.py's concern."""
    assert "--max-turns" not in SCRIPT.read_text()
