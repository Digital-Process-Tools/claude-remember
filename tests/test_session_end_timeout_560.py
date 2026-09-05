"""SessionEnd hook must declare a per-hook `timeout` in hooks/hooks.json (#560).

Claude Code's SessionEnd budget defaults to 1.5 seconds, shared across every
hook registered for that event (Claude Code hooks reference, "Timeouts",
checked 2026-09). session-end-hook.sh forks its real flush into the
background and returns immediately once it has forked -- but the synchronous
preamble before that fork (lib-clock.sh, resolve-paths.sh, detect-tools.sh's
python/jq detection, bootstrap-dirs.sh, log.sh) was measured at ~3.4s on a
reporter's Windows/Git-Bash machine (#560), well past the 1.5s budget, so
Claude Code cancelled the hook before it ever reached the fork. Declaring a
`timeout` on the hook *asks* Claude Code to raise that shared budget to match.

IMPORTANT CAVEAT the fix does not fully resolve (documented in the pull
request and the docs update, not re-litigated in this test): the same
reference states "Timeouts set on plugin-provided hooks don't raise the
budget" -- and this hook ships as `hooks/hooks.json` inside a plugin, exactly
the location that clause names. Declaring `timeout` here is still correct
practice (it is honored by settings-file-registered copies of this hook, and
does no harm here even where the plugin-budget carve-out applies), but it is
not, by itself, a load-bearing fix for a plugin install -- see the docs
update and the changelog fragment for the actionable remedy
(CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS). This test only pins the one thing
this diff can enforce mechanically: the manifest declares a timeout.

The bar: would this test still pass if hooks.json were left unchanged? No --
today's manifest has no `timeout` key anywhere, so this fails for the right
reason (KeyError / missing field) before the fix, and passes once
`"timeout": 10` is added to the SessionEnd entry.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"


def _session_end_hooks():
    data = json.loads(HOOKS_JSON.read_text())
    groups = data["hooks"]["SessionEnd"]
    for group in groups:
        yield from group.get("hooks", [])


def test_session_end_hook_declares_a_sane_timeout():
    """Every SessionEnd command hook must declare a numeric `timeout` field.

    Sane window: more than the reporter's measured ~3.4s (so it is not a
    no-op) and no more than 60s (the documented ceiling Claude Code will
    ever raise the SessionEnd budget to, per the same reference).
    """
    hooks = list(_session_end_hooks())
    assert hooks, "no SessionEnd command hooks found -- test/manifest drifted"
    for hook in hooks:
        assert "timeout" in hook, (
            f"SessionEnd hook {hook.get('command')!r} declares no `timeout` -- "
            f"Claude Code cancels SessionEnd hooks at a 1.5s default budget "
            f"(#560), and this hook's own preamble was measured well past "
            f"that on a slow Windows/Git-Bash machine"
        )
        timeout = hook["timeout"]
        assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool), (
            f"SessionEnd hook timeout must be numeric, got {timeout!r}"
        )
        assert 3.4 < timeout <= 60, (
            f"SessionEnd hook timeout={timeout!r} is not in the sane window "
            f"(>3.4s measured preamble cost, <=60s documented ceiling)"
        )


def test_only_session_end_declares_a_timeout():
    """Other hook events already get a 600s (or 30s for UserPromptSubmit)
    default per the same reference -- a `timeout` field only belongs on the
    one event whose shared default (1.5s) is too tight for this plugin's own
    hook to reliably fork its background flush inside.
    """
    data = json.loads(HOOKS_JSON.read_text())
    for event, groups in data["hooks"].items():
        if event == "SessionEnd":
            continue
        for group in groups:
            for hook in group.get("hooks", []):
                assert "timeout" not in hook, (
                    f"{event} hook {hook.get('command')!r} declares a "
                    f"`timeout` -- that event does not share SessionEnd's "
                    f"tight 1.5s default and does not need one"
                )
