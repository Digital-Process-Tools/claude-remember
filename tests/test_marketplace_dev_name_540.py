"""The in-repo Codex marketplace's own name must not collide with the
published catalogue (#540).

`.agents/plugins/marketplace.json` is a *development* catalogue -- one
plugin, local source, `./` -- that lets a maintainer `codex plugin
marketplace add .` a checkout. It used to declare itself `"name":
"dpt-plugins"`, which is also the name of the *published* Codex catalogue in
Digital-Process-Tools/codex-marketplace. On a machine that has added both,
`remember@dpt-plugins` means two different things depending on which session
you are in. The fix renames the in-repo catalogue to `remember-dev`, the
same `<plugin>-dev` shape claude-jit-context already ships as
`claude-jit-context-dev`.

This test would still pass if the rename had never happened, if it only
asserted the field was present -- so it pins the exact value, and it pins
that the value is NOT the published catalogue's name, so a future revert
back to `dpt-plugins` fails loudly here rather than silently colliding
again.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"


def test_marketplace_name_is_the_dev_catalogue_not_the_published_one():
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    assert data.get("name") == "remember-dev", (
        f"marketplace.json 'name' is {data.get('name')!r}, expected 'remember-dev' -- "
        "the in-repo dev catalogue must not answer to the same name as the "
        "published Digital-Process-Tools/codex-marketplace catalogue (#540)"
    )
    assert data.get("name") != "dpt-plugins", (
        "marketplace.json 'name' must not be 'dpt-plugins' -- that name now "
        "belongs to the published Codex catalogue (#540)"
    )
