"""A non-object JSON document must not crash the status line (#727).

Claude Security scan findings F14/F15: ``repo_version`` and ``_latest_release``
each ``json.loads(...).get(...)`` a document that is not guaranteed to be a
dict. ``json.loads`` happily returns a list/str/int/``None`` for a
syntactically valid but non-object JSON document -- ``[]``, ``"1.0"``, ``1``,
``null`` -- and ``.get`` on any of those raises ``AttributeError``, which
neither function's own ``except`` tuple catches. For ``repo_version`` that
aborts the whole status-line render (blank output). For ``_latest_release``
that aborts the detached refresh mid-way, so the cache is never rewritten and
the board freezes at stale counts.

``repo_config``, ``installed_plugins`` and ``_installed_plugin_root`` share
the exact same crash class over ``.oss.json``/``installed_plugins.json``
(found during this issue's own recon, not named in the original report) and
are fixed and pinned alongside the two named findings.

Each positive (non-dict-does-not-raise) case is paired with a positive
control (a well-formed document still yields its real value) -- a fix that
returns ``None``/``{}`` unconditionally, for every input, would pass the
non-crash assertions here for the wrong reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".oss"))

import statusline

# --------------------------------------------------------------------- F14: repo_version

def test_non_object_plugin_json_does_not_raise(tmp_path):
    manifest_dir = tmp_path / ".claude-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text("[]")

    result = statusline.repo_version(tmp_path)  # must not raise AttributeError

    assert result is None or isinstance(result, str)


def test_well_formed_plugin_json_still_yields_its_version(tmp_path):
    manifest_dir = tmp_path / ".claude-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text(json.dumps({"version": "1.2.3"}))

    assert statusline.repo_version(tmp_path) == "1.2.3"


# ----------------------------------------------------------------- F15: _latest_release

def test_non_object_remote_manifest_does_not_raise(monkeypatch):
    import base64

    encoded = base64.b64encode(b"[1, 2, 3]").decode("ascii")
    monkeypatch.setattr(statusline, "_run", lambda command, timeout=25: encoded)

    assert statusline._latest_release("acme/widgets") is None


def test_well_formed_remote_manifest_still_yields_its_version(monkeypatch):
    import base64

    payload = json.dumps({"version": "9.9.9"}).encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    monkeypatch.setattr(statusline, "_run", lambda command, timeout=25: encoded)

    assert statusline._latest_release("acme/widgets") == "9.9.9"


# ---------------------------------------------------------- sibling: repo_config

def test_non_object_oss_json_does_not_propagate_as_a_dict(tmp_path):
    (tmp_path / ".oss.json").write_text("null")

    assert statusline.repo_config(tmp_path) == {}


def test_well_formed_oss_json_still_yields_its_fields(tmp_path):
    (tmp_path / ".oss.json").write_text(json.dumps({"repo": "acme/widgets"}))

    assert statusline.repo_config(tmp_path) == {"repo": "acme/widgets"}


# ------------------------------------------------------ sibling: installed_plugins

def test_non_object_installed_plugins_json_does_not_raise(tmp_path):
    (tmp_path / "installed_plugins.json").write_text("42")

    result = statusline.installed_plugins(str(tmp_path), plugins_root=tmp_path)

    assert result == {}


def test_well_formed_installed_plugins_json_still_yields_its_entries(tmp_path):
    project = str(tmp_path / "project")
    doc = {
        "plugins": {
            "widgets@acme": [
                {"version": "1.0.0", "scope": "user"},
            ]
        }
    }
    (tmp_path / "installed_plugins.json").write_text(json.dumps(doc))

    result = statusline.installed_plugins(project, plugins_root=tmp_path)

    assert result["widgets"]["version"] == "1.0.0"


def test_non_object_per_plugin_manifest_does_not_raise(tmp_path):
    """Self-review finding: installed_plugins() reads a SECOND JSON document per
    entry -- the installed plugin's own .claude-plugin/plugin.json, at the
    installPath an installed_plugins.json entry names -- and called `.get()`
    on it with no isinstance guard, the same crash class as the top-level
    document this module's other fixes already cover."""
    project = str(tmp_path / "project")
    install_path = tmp_path / "installed-widgets"
    (install_path / ".claude-plugin").mkdir(parents=True)
    (install_path / ".claude-plugin" / "plugin.json").write_text("null")
    doc = {
        "plugins": {
            "widgets@acme": [
                {
                    "version": "1.0.0",
                    "scope": "user",
                    "installPath": str(install_path),
                },
            ]
        }
    }
    (tmp_path / "installed_plugins.json").write_text(json.dumps(doc))

    result = statusline.installed_plugins(project, plugins_root=tmp_path)  # must not raise

    assert result["widgets"]["version"] == "1.0.0"
    assert result["widgets"]["repository"] is None


# ------------------------------------------------ sibling: _installed_plugin_root

def test_non_object_installed_plugins_json_root_lookup_does_not_raise(tmp_path):
    (tmp_path / "installed_plugins.json").write_text('"a string, not a dict"')

    result = statusline._installed_plugin_root(
        str(tmp_path / "project"), "widgets", plugins_root=tmp_path
    )

    assert result is None


def test_well_formed_installed_plugins_json_root_lookup_still_resolves(tmp_path):
    project = str(tmp_path / "project")
    doc = {
        "plugins": {
            "widgets@acme": [
                {"installPath": "/opt/widgets", "scope": "user"},
            ]
        }
    }
    (tmp_path / "installed_plugins.json").write_text(json.dumps(doc))

    result = statusline._installed_plugin_root(project, "widgets", plugins_root=tmp_path)

    assert result == "/opt/widgets"
