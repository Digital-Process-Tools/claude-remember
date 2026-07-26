"""Shared test environment guards.

Tests build sandboxes by pointing HOME at a temp directory. That is only half
a sandbox: CLAUDE_CONFIG_DIR relocates Claude Code's whole config tree, so a
developer who exports it (per-project accounts, direnv) had the real variable
follow the code straight out of the fixture and into their actual store.

It stayed invisible while the plugin hardcoded ~/.claude and simply ignored the
variable — the leak and the bug cancelled out. Honouring it (#166) exposed both
at once: five existing tests started reading the developer's real config tree.
Cleared here rather than in each fixture, so the next sandbox is hermetic
without anyone having to remember why.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_claude_config_dir(monkeypatch):
    """Keep the ambient CLAUDE_CONFIG_DIR out of every test.

    A test that wants it set does so explicitly, which then means what it says
    rather than inheriting whatever the developer happens to run.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
