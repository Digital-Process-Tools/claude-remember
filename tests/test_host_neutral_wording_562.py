"""#562 -- README prose said "Claude Code" in sentences that are true of any
coding agent this project supports (Claude Code, Codex), and said "Claude
Remember" in prose after the first mention on a page where "Remember" reads
better. Two wording rules, both host-neutral prose only -- no manifest, no
install command, no path, no hook-event name is touched.

Would this test pass if nothing changed? No: every assertion below names an
exact rewritten sentence that does not exist in the pre-#562 tree (confirmed
against the base commit's README.md and docs/git-backup-security.md via
`git show`), and separately asserts the narrow, Claude-Code-specific
sentences the issue says must survive untouched still do.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
GIT_BACKUP_SECURITY = (REPO_ROOT / "docs" / "git-backup-security.md").read_text(encoding="utf-8")
DIAGNOSTICS = (REPO_ROOT / "docs" / "diagnostics.md").read_text(encoding="utf-8")
CONFIGURATION = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")


def test_readme_opening_paragraphs_are_host_neutral():
    """The opening pitch is true of any coding agent this project supports,
    not just Claude Code -- it should say so, and never say "LLM" instead
    (the hooks and paths it describes belong to the CLI, not the model)."""
    assert "Your coding agent starts every session blank." in README
    assert "Claude Code starts every session blank." not in README
    assert "your coding agent develops continuity" in README
    assert "your Claude Code instance develops continuity" not in README
    assert "LLM" not in README


def test_readme_trust_model_says_coding_agent_not_claude_code():
    assert "no new attack surface beyond your coding agent itself" in README
    assert "no new attack surface beyond Claude Code itself" not in README
    assert "like any other hook your coding agent runs" in README
    assert "like any other Claude Code hook" not in README


def test_readme_keeps_claude_code_where_the_sentence_is_claude_code_specific():
    """The install section, the hook-name table and the badge naming Claude
    Code as a specific host are not host-neutral sentences -- #562 says these
    must survive verbatim."""
    assert "**Claude Code**\n" in README
    assert "Restart Claude Code afterwards; hooks are read at session start" in README
    assert "Claude Code kills a hook at 60s" not in README  # this sentence lives in docs/configuration.md, not README
    assert "| Claude Code / Codex | Gemini CLI | Script | Purpose |" in README


def test_readme_full_name_appears_once_then_short_name():
    """Full name on first mention per page, "Remember" after that (#562)."""
    assert README.count("Claude Remember") == 1
    assert "Remember fixes that." in README or "Claude Remember fixes that." in README


def test_git_backup_security_says_coding_agent_for_the_general_trust_claim():
    assert (
        "no new attack surface beyond what your coding agent already needs "
        "to run on your machine" in GIT_BACKUP_SECURITY
    )
    assert "beyond what Claude Code already needs to run on your machine" not in GIT_BACKUP_SECURITY
    assert "everything you discuss with your coding agent" in GIT_BACKUP_SECURITY


def test_git_backup_security_keeps_claude_code_for_the_claude_specific_path():
    """`~/.claude/**` and the `~/.claude/plugins/cache/` comparison name a
    real Claude-Code-only path -- #562 says these stay verbatim."""
    assert "`~/.claude/**` — change which hooks Claude Code runs." in GIT_BACKUP_SECURITY
    assert "Same as Claude Code's own hook directory." in GIT_BACKUP_SECURITY


def test_diagnostics_hook_errors_log_is_host_neutral():
    """bootstrap-dirs.sh wires every host's hook scripts, not only Claude
    Code's -- see hooks/hooks.json and .gemini/hooks/hooks.json both sourcing
    it via the shared scripts/ tree."""
    assert "every coding agent hook's stderr" in DIAGNOSTICS
    assert "every Claude Code hook's stderr" not in DIAGNOSTICS


def test_configuration_generic_session_behaviour_is_host_neutral():
    assert "Useful when your coding agent runs from a non-git directory" in CONFIGURATION
    assert "Useful when Claude Code runs from a non-git directory" not in CONFIGURATION
    assert "several coding agent sessions running at once in one project" in CONFIGURATION
    assert "several Claude Code sessions running at once in one project" not in CONFIGURATION


def test_configuration_keeps_claude_code_for_the_measured_hook_timeout():
    """Claude Code's own 60s hook kill is a measurement taken specifically on
    Claude Code, not a host-neutral fact -- #562 says this stays verbatim."""
    assert "Claude Code kills a hook at 60s of its own accord" in CONFIGURATION
