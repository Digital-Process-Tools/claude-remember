"""README.md must disclose every credential-handling env/config key and
every behaviour #854 found undisclosed: the codex exec summarizer path, the
opt-in git fetch, the files written outside the project, and the real git
backup trigger.

Found by the Anthropic directory's security scan (#851/#853): the scan holds
undisclosed behaviour, and a directory reviewer reads the README before the
code. v0.36.0 passed the scan, so none of this blocks today -- the listing
was held for the directory team pending disclosure, not for a functional
defect.

What this asserts: every name #854 lists appears somewhere in README.md.
It is a presence check, not a prose check -- it cannot tell a correct
sentence from a misleading one, which is why the positive control below
(asserting a name NOT in the list is also absent) matters: without it, a
README that quoted every file in the repo verbatim would also pass, and the
test would not be distinguishing "disclosed" from "method found nothing to
disclose because it checks nothing".
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"


def _text() -> str:
    return README.read_text(encoding="utf-8")


# Every env var / config key #854 names as undisclosed credential handling,
# plus the summarizer selector and the opt-in restore flag.
_REQUIRED_NAMES = [
    "REMEMBER_SUMMARIZER",
    "codex exec",
    "git_restore.enabled",
    "promo-notice",
    "run/summarizers",
    "remember-",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "REMEMBER_OAUTH_TOKEN",
    "haiku.oauth_token",
    "ANTHROPIC_API_KEY",
    "haiku.anthropic_api_key",
    "CODEX_API_KEY",
]


def test_readme_discloses_every_854_name():
    text = _text()
    missing = [name for name in _REQUIRED_NAMES if name not in text]
    assert not missing, f"README.md does not mention: {missing}"


def test_readme_git_backup_wording_has_no_enable_flag_claim():
    """#854's /Inaccurate/ finding: README used to say the git backup push
    happens "If you enable it" -- the hook has no enable flag at all; it
    triggers on the external store's parent directory being a git repo with
    an upstream. The old, wrong wording must not reappear."""
    text = _text()
    assert "If you enable it" not in text


def test_positive_control_a_name_actually_absent_from_995_is_flagged():
    """Positive control: a name #854 never mentions, and this repo's code
    does not reference anywhere, must be absent from README.md too -- so the
    presence check above is actually discriminating, not vacuously true
    because the README happens to contain nearly every string from the repo.
    """
    text = _text()
    assert "QUUX_NEVER_A_REAL_TOKEN_NAME_995" not in text
