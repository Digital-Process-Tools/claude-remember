"""detect_host() must recognise a real Codex process (#463).

#460 shipped Codex routing keyed to ``signature_vars=("CODEX_HOME",
"PLUGIN_ROOT")``. Both pass in a hand-built test environment, because a test
that constructs its own environment can only confirm the implementation
against itself. Neither is ever exported by Codex to a child process --
``CODEX_HOME`` is a configuration path Codex *reads*, not a signature it
exports -- so ``detect_host()`` could not return ``CODEX`` on any real
machine while #460's own suite stayed green.

The fix is pinned against ``tests/fixtures/codex-env-463.txt``, a verbatim
capture from a live ``codex exec`` session, precisely so a future regression
of the same shape (choosing a plausible-looking but unexported variable
again) cannot pass by constructing a fixture that agrees with itself.

#465: the fixture above was captured from a Codex *tool shell*, not from
the SessionEnd *hook* process that actually runs Remember's summarizer --
those are different children, and a live capture from inside the hook shows
CODEX_SESSION_ID/CODEX_THREAD_ID do not survive into it. ``detect_host()``
itself is unchanged and these assertions still hold (it is a correct
description of what a tool-shell environment looks like); what changed is
that ``pipeline.haiku._choose_summarizer_provider()`` no longer calls it for
"auto" -- see tests/test_codex_hook_transcript_465.py and
pipeline/haiku.py's own note.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import host as _host

FIXTURE = Path(__file__).parent / "fixtures" / "codex-env-463.txt"


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key] = value
    return env


def test_real_codex_environment_is_detected_as_codex():
    """The positive control: a recorded real Codex process IS detected.

    This is the fixture with CLAUDE_CODE_* stripped out, isolating the
    signature-variable defect from the separate nested-signature question
    below.
    """
    env = _load_env(FIXTURE)
    for var in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"):
        env.pop(var, None)
    assert _host.detect_host(env).name == "codex"


def test_codex_home_alone_is_no_longer_a_signature():
    """The must-not-fire half paired with the positive control above:
    CODEX_HOME being SET must not, on its own, make detect_host() answer
    CODEX -- it is a value Codex reads, never one it exports, so treating
    it as a signature is exactly the #463 defect. An ambient CODEX_HOME
    left over from a config file must not falsely identify the host."""
    env = {"CODEX_HOME": "/some/config/path"}
    assert _host.detect_host(env).name != "codex"


def test_plugin_root_alone_is_no_longer_a_signature():
    """PLUGIN_ROOT is a compatibility alias for the plugin install
    directory (see plugin_root_vars), never a Codex signature -- the same
    class of mistake CODEX_HOME was."""
    env = {"PLUGIN_ROOT": "/some/plugin/install/dir"}
    assert _host.detect_host(env).name != "codex"


def test_real_codex_environment_with_inherited_claude_code_signature():
    """The fixture as actually captured: a Codex session launched FROM a
    Claude Code session inherits CLAUDE_CODE_ENTRYPOINT/SESSION_ID from its
    parent's environment. Both hosts' signatures are genuinely present at
    once. detect_host() must answer deterministically -- see the module
    docstring's "two signatures at once" note for why CLAUDE_CODE keeps
    registry precedence here rather than guessing "innermost" from flat
    environment variables that carry no ancestry information."""
    env = _load_env(FIXTURE)
    assert _host.detect_host(env).name == "claude-code"
