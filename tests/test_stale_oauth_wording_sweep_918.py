"""Regression tests for #918: stale `haiku.oauth_token` / `REMEMBER_OAUTH_TOKEN`
wording survives in four code-comment sites and two docs pages after #860
removed both as auth sources.

#860 (round 3/4) made `haiku.oauth_token` and the `REMEMBER_OAUTH_TOKEN`
environment variable inert everywhere -- neither is read for authentication
on any host any more, and `docs/configuration.md`'s own current entries say
so plainly ("Removed entirely ... not read for anything ... there is
nothing to migrate it to"). Six sites found by curate while disposing of two
release-audit trap.d fragments still described the opposite:

- `hooks.d/after_save/50-git-backup.sh`, `hooks.d/before_session_start/50-git-restore.sh`,
  `scripts/lib-memory-dir.sh` and `scripts/log.sh` still called `haiku.oauth_token`
  "a live ... OAuth credential" in comments/log lines.
- `docs/git-backup-security.md` and `docs/external-storage-mode.md` still told
  users to set `REMEMBER_OAUTH_TOKEN` as a *working* mitigation ("it never
  touches disk inside the backup store at all").

Per this repo's CLAUDE.md ("a negative assertion needs a positive control"),
each "must not still say X" check is paired with a "the check actually fires
on X" fixture below, so this test cannot pass merely because the check never
fires.
"""

from __future__ import annotations

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LIVE_CREDENTIAL_FILES = [
    "hooks.d/after_save/50-git-backup.sh",
    "hooks.d/before_session_start/50-git-restore.sh",
    "scripts/lib-memory-dir.sh",
    "scripts/log.sh",
]

_WORKING_MITIGATION_FILES = [
    "docs/git-backup-security.md",
    "docs/external-storage-mode.md",
]

# How far either side of "oauth_token" to look for "live" describing it as a
# currently-active credential. Wide enough to span a line wrap or a
# backticked name, narrow enough not to fire on an unrelated "live" far away
# in a long comment block.
_WINDOW = 80


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _find_live_credential_claim(text: str):
    """Return the first window where "oauth_token" (any case) co-occurs with
    "live", collapsing whitespace so a line-wrapped phrase still matches."""
    for match in re.finditer(r"oauth_token", text, re.IGNORECASE):
        start = max(0, match.start() - _WINDOW)
        end = min(len(text), match.end() + _WINDOW)
        window = re.sub(r"\s+", " ", text[start:end].lower())
        if "live" in window:
            return match.group(0)
    return None


def test_no_code_comment_still_calls_oauth_token_live():
    offenders = []
    for rel_path in _LIVE_CREDENTIAL_FILES:
        hit = _find_live_credential_claim(_read(rel_path))
        if hit is not None:
            offenders.append(rel_path)
    assert not offenders, (
        f"these files still describe haiku.oauth_token as a live credential "
        f"after #860 made it inert everywhere: {offenders} (#918)"
    )


def test_positive_control_fires_on_live_credential_phrasing():
    # MUST fire: without this, a broken check (one that never matches)
    # would make the assertion above pass regardless of file content.
    phrasings = [
        "a documented home for haiku.oauth_token -- a live coding-agent OAuth credential.",
        "config.json -- which can carry a live haiku.oauth_token -- exactly as before",
        "if it carried a live haiku.oauth_token, treat that credential as compromised",
    ]
    for fixture in phrasings:
        assert _find_live_credential_claim(fixture) is not None, (
            f"positive control failed to fire on known-stale phrasing: {fixture!r}"
        )


def _find_working_mitigation_claim(text: str):
    """Return the first window where "REMEMBER_OAUTH_TOKEN" co-occurs with
    "never touches disk" -- i.e. the doc still frames it as a currently
    effective mitigation, rather than inert like `haiku.oauth_token` is."""
    for match in re.finditer(r"REMEMBER_OAUTH_TOKEN", text):
        start = max(0, match.start() - 200)
        end = min(len(text), match.end() + 200)
        window = re.sub(r"\s+", " ", text[start:end].lower())
        if "never touches disk" in window:
            return match.group(0)
    return None


def test_no_doc_still_presents_remember_oauth_token_as_working_mitigation():
    offenders = []
    for rel_path in _WORKING_MITIGATION_FILES:
        hit = _find_working_mitigation_claim(_read(rel_path))
        if hit is not None:
            offenders.append(rel_path)
    assert not offenders, (
        f"these docs still tell users to set REMEMBER_OAUTH_TOKEN as a "
        f"working mitigation, when it is read for nothing on any host after "
        f"#860: {offenders} (#918)"
    )


def test_positive_control_fires_on_working_mitigation_phrasing():
    # MUST fire: the paired positive control for the assertion above.
    phrasings = [
        (
            "prefer the REMEMBER_OAUTH_TOKEN environment variable over config.json "
            "for this key -- it never touches disk inside the backup store at all."
        ),
        (
            "put it in the REMEMBER_OAUTH_TOKEN environment variable instead, which "
            "never touches disk inside the backup store."
        ),
    ]
    for fixture in phrasings:
        assert _find_working_mitigation_claim(fixture) is not None, (
            f"positive control failed to fire on known-stale phrasing: {fixture!r}"
        )
