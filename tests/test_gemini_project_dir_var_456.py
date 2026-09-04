"""Gemini CLI's own hook environment documents `CLAUDE_PROJECT_DIR` (#456).

`GEMINI = Host(name="gemini-cli")` in `pipeline/host.py` used to declare
`project_dir_vars=()`, on the strength of a docstring claim that "Gemini CLI
documents no environment variables for command hooks at all". That claim is
false: the installed `@google/gemini-cli` 0.57.0 package's own
`bundle/docs/hooks/index.md`, under `### Environment variables`, lists five
hook-process environment variables, and one of them is `CLAUDE_PROJECT_DIR`
itself, spelled `(Alias) Provided for compatibility.` -- the exact name
`CODEX` already carries in its own `project_dir_vars` for the same reason.

This is a documentation-only correction, not a claim that Gemini CLI's
`CLAUDE_PROJECT_DIR` alias has been observed live: no `gemini` binary runs in
CI (the same limit `tests/test_gemini_manifest_456.py` states for the
manifest), and the wider question of whether the rest of this repo's
`CLAUDE_PROJECT_DIR`-unset branches (`scripts/resolve-paths.sh`,
`scripts/lib-env-cache.sh`, `scripts/user-prompt-hook.sh`, and others) behave
correctly now that Gemini is known to set it stays open -- filed as its own
follow-up rather than fixed here, since it reaches well outside this file.
"""

from __future__ import annotations

from pipeline.host import CODEX, GEMINI


def test_gemini_recognises_claude_project_dir_alias():
    """Gemini CLI's own bundled docs list `CLAUDE_PROJECT_DIR` as a
    compatibility alias for `GEMINI_PROJECT_DIR` -- the same alias CODEX
    already reads project_dir from. GEMINI must read it too."""
    env = {"CLAUDE_PROJECT_DIR": "/some/project"}
    assert GEMINI.project_dir(env) == "/some/project"


def test_gemini_project_dir_vars_matches_codex_shape():
    """Not a claim the two hosts are identical -- CODEX's own comment block
    explains why `CLAUDE_PROJECT_DIR` is its only entry (no Codex-native
    project-dir name exists). Gemini's bundled docs give the same reason:
    `CLAUDE_PROJECT_DIR` is documented there as an alias too, with no
    Gemini-native project-dir variable name alongside it."""
    assert GEMINI.project_dir_vars == CODEX.project_dir_vars == ("CLAUDE_PROJECT_DIR",)


def test_gemini_still_declares_no_plugin_root_var():
    """Unlike project_dir, Gemini's bundled docs name no plugin-root
    variable at all -- not `PLUGIN_ROOT`, not `CLAUDE_PLUGIN_ROOT`. This one
    stays empty; only project_dir_vars was wrong."""
    assert GEMINI.plugin_root_vars == ()
