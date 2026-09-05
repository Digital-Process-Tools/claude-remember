"""#562 -- README prose said "Claude Code" in sentences that are true of any
coding agent this project supports (Claude Code, Codex), and #562 also asked
to fold "Claude Remember" to "Remember" after a page's first mention, where
a page had drifted from that convention. Two wording rules, both host-neutral
prose only -- no manifest, no install command, no path, no hook-event name is
touched.

Would this test pass if nothing changed? Almost every assertion below names
an exact rewritten sentence that does not exist in the pre-#562 tree
(confirmed against the base commit's README.md and four docs/ pages via
`git show`), and separately asserts the narrow, Claude-Code-specific
sentences the issue says must survive untouched still do. The one exception
is test_readme_full_name_appears_once_then_short_name: on inspection every
page already held "Claude Remember" at most once, so there was no page left
to fold -- that function pins the already-correct convention as a regression
guard rather than a red-before-green check, and its docstring says so rather
than overclaiming, a distinction a reviewer had to point out because an
earlier version of this docstring claimed every assertion here was
red-before-green when this one was not (caught in self-review before #562's
commit)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
GIT_BACKUP_SECURITY = (REPO_ROOT / "docs" / "git-backup-security.md").read_text(encoding="utf-8")
DIAGNOSTICS = (REPO_ROOT / "docs" / "diagnostics.md").read_text(encoding="utf-8")
CONFIGURATION = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
WINDOWS = (REPO_ROOT / "docs" / "windows.md").read_text(encoding="utf-8")
EXTERNAL_STORAGE_MODE = (REPO_ROOT / "docs" / "external-storage-mode.md").read_text(encoding="utf-8")
MEASURING_LOCK_HOLD_TIMES = (REPO_ROOT / "docs" / "measuring-lock-hold-times.md").read_text(encoding="utf-8")


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
    """Full name on first mention per page, "Remember" after that (#562).

    This is a regression guard, not a red-before-green check: README.md
    already held "Claude Remember" exactly once before #562, so there was
    no second mention to fold here. It still catches a future page picking
    the full name back up on a second mention."""
    assert README.count("Claude Remember") == 1


def test_readme_session_start_injection_is_host_neutral():
    """The `SessionStart` hook is true of Claude Code and Codex alike (see
    the hooks table further down README.md) -- the sentence introducing it
    must not narrow "context" down to "Claude's context" specifically,
    which would misdescribe a Codex install running a different model."""
    assert "injects into your coding agent's context" in README
    assert "injects into Claude's context" not in README


def test_windows_hook_shell_wording_is_host_neutral():
    assert "resolvable from the shell your coding agent launches hooks in" in WINDOWS
    assert "resolvable from the shell Claude Code launches hooks in" not in WINDOWS


def test_external_storage_mode_hook_environment_wording_is_host_neutral():
    assert "in the environment your coding agent launches hooks in" in EXTERNAL_STORAGE_MODE
    assert "in the environment Claude Code launches hooks in" not in EXTERNAL_STORAGE_MODE


def test_measuring_lock_hold_times_hook_shell_wording_is_host_neutral():
    assert "in the shell your coding agent launches hooks from" in MEASURING_LOCK_HOLD_TIMES
    assert "in the shell Claude Code launches hooks from" not in MEASURING_LOCK_HOLD_TIMES


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
