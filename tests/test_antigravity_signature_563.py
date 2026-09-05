"""detect_host() must recognise a real Antigravity CLI (agy) process (#563).

Mirrors tests/test_codex_signature_463.py, for the same reason: a signature
the implementation invents and a fake environment that agrees with itself
prove nothing. This one is pinned against a verbatim, live capture instead
(tests/fixtures/antigravity-env-563.txt) -- ANTIGRAVITY_CONVERSATION_ID,
found only by dumping a real `agy` hook process's own environment (#553's
last comment confirms Antigravity hooks fire at all; this fixture goes one
step further and shows what the firing PROCESS's environment actually
contains, which #553 never captured).

Observed on macOS darwin/arm64, `agy` 1.1.27, this session, 2026-09-05.
Nothing here is claimed for any other platform or `agy` version.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import host as _host

FIXTURE = Path(__file__).parent / "fixtures" / "antigravity-env-563.txt"


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key] = value
    return env


def test_real_antigravity_environment_is_detected_as_antigravity():
    """The positive control: a recorded real `agy` hook process IS detected.

    Isolated from the CLAUDE_CODE_* signature this fixture also carries (the
    nested case is its own test below), the same way
    test_real_codex_environment_is_detected_as_codex isolates CODEX.
    """
    env = _load_env(FIXTURE)
    for var in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"):
        env.pop(var, None)
    assert _host.detect_host(env).name == "antigravity"


def test_ambient_conversation_id_shaped_value_alone_is_not_enough():
    """The must-not-fire half paired with the positive control above: an
    unrelated variable that merely LOOKS like a conversation id must not,
    on its own, make detect_host() answer "antigravity" for the wrong
    reason -- this only proves the signature NAME matters, not that any
    UUID-shaped value flips the switch. A genuinely absent signature must
    answer something else."""
    env = {"SOME_OTHER_CONVERSATION_ID": "abdde66c-1153-4a56-bba2-7db93a9a2614"}
    assert _host.detect_host(env).name != "antigravity"


def test_no_antigravity_signature_is_not_detected_as_antigravity():
    """A plain, empty environment must not be mistaken for Antigravity."""
    assert _host.detect_host({}).name != "antigravity"


def test_real_antigravity_environment_with_inherited_claude_code_signature():
    """The fixture as actually captured: this `agy` session was launched
    FROM a Claude Code session, so it inherits CLAUDE_CODE_ENTRYPOINT/
    CLAUDE_CODE_SESSION_ID from its parent's environment. Both hosts'
    signatures are genuinely present at once, the same shape #463 documents
    for Codex. detect_host() must still answer deterministically; registry
    order (CLAUDE_CODE first) decides it, same reasoning as the Codex case."""
    env = _load_env(FIXTURE)
    assert _host.detect_host(env).name == "claude-code"


def test_antigravity_host_declares_no_plugin_root_or_project_dir_vars():
    """Neither is documented or observed for Antigravity (#563) -- unlike
    Codex's PLUGIN_ROOT alias or Gemini's CLAUDE_PROJECT_DIR alias, nothing
    in the fixture or in Antigravity's own hook payload names either kind of
    path via an environment variable. Declaring one here that was never
    observed would be exactly the #463 mistake (a plausible-looking but
    unexported variable) one host over."""
    assert _host.ANTIGRAVITY.plugin_root_vars == ()
    assert _host.ANTIGRAVITY.project_dir_vars == ()
