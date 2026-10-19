"""The directory's "Uses a credential from the user's machine" finding came back on
v0.42.3 (release 8803994) after v0.42.2 (release 55e1520) shipped without it.

Observed (the listed plugin's own version list): v0.42.2 clean and Approved, v0.42.3
flagged, nothing else in the finding list changed. The finding is a READ paired with a
SEND anywhere in the plugin (docs/releasing.md, "MCP_FORWARDS_CREDENTIAL_ENV").
v0.42.2 already shipped senders -- the backup hook's push to its remote was cited as the
send side in earlier rounds -- so a new send alone cannot have produced the finding; a
new read had to appear. Reasoned from that: the only credential-shaped text the
55e1520..8803994 release diff adds is the #974/#989 rotate advice in
hooks.d/after_save/50-git-backup.sh, which names `haiku.oauth_token` and the command
`claude setup-token` -- in the same file as that push. `claude setup-token` in a
user-facing string has been cited verbatim as the read before (#898, probe hE).

So this pins: no shipped shell script in the built release tree names a credential
(the checker's own CREDENTIAL_USE pattern, plus `setup-token`). config.example.json
is outside that scope on purpose: it named `oauth_token` on v0.42.2 too, which cleared.

Positive controls: the same needle does find `oauth_token` in the built tree's
config.example.json (so the build and the needle are real), and it finds the exact
v0.42.3 wording. The Windows hidden-window launcher (#1002) is pinned as still shipped,
because the fix is on the read side and must not touch it.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / ".github" / "scripts"
BUILD = SCRIPTS / "build_release_tree.py"
CHECK = SCRIPTS / "check_release_tree.py"

SHELL_DIRS = ("hooks", "hooks.d", "scripts")

V0423_WORDING = (
    "If that config.json ever carried a haiku.oauth_token, "
    "treat it as compromised and rotate it (claude setup-token)."
)


def _needle() -> re.Pattern:
    spec = importlib.util.spec_from_file_location("check_release_tree_cred_text", CHECK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree_cred_text"] = mod
    spec.loader.exec_module(mod)
    return re.compile(mod.CREDENTIAL_USE.pattern + r"|setup-token")


@pytest.fixture(scope="module")
def built_tree(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("release") / "tree"
    result = subprocess.run(
        [sys.executable, str(BUILD), "--repo", str(REPO_ROOT), "--ref", "HEAD",
         "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def _shell_files(tree: Path) -> list:
    files = []
    for top in SHELL_DIRS:
        base = tree / top
        if base.is_dir():
            files.extend(p for p in sorted(base.rglob("*.sh")) if p.is_file())
    return files


def test_no_shipped_shell_script_names_a_credential(built_tree):
    needle = _needle()
    shells = _shell_files(built_tree)
    assert shells, "the built tree has no shell scripts at all -- the scan saw nothing"
    hits = []
    for path in shells:
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if needle.search(line):
                rel = path.relative_to(built_tree).as_posix()
                hits.append(f"{rel}:{n}: {line.strip()[:160]}")
    assert hits == [], (
        "a shipped shell script names a credential -- the read half of the directory's "
        "'Uses a credential from the user's machine' pair (v0.42.3 regression):\n"
        + "\n".join(hits)
    )


def test_positive_control_needle_finds_the_config_example_key(built_tree):
    text = (built_tree / "config.example.json").read_text(encoding="utf-8")
    assert _needle().search(text), "needle no longer sees oauth_token in config.example.json"


def test_positive_control_needle_finds_the_v0423_wording():
    found = {m.group(0) for m in _needle().finditer(V0423_WORDING)}
    assert {"oauth_token", "setup-token"} <= found, found


def test_hidden_window_launcher_still_ships(built_tree):
    vbs = built_tree / "scripts" / "windows-hidden-run.vbs"
    assert vbs.is_file(), "#1002's hidden-window launcher left the release tree"
    hook = (built_tree / "scripts" / "post-tool-hook.sh").read_text(encoding="utf-8")
    assert "wscript.exe //B" in hook, "post-tool-hook.sh no longer launches through wscript"
