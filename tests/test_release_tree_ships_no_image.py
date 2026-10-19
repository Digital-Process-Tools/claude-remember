"""The release tree ships no image or font file, and plugin.json names no icon.

v0.42.0 added `.claude-plugin/icon.png` and `"icon"` in plugin.json (#865, PR #922).
Every version since carries the Anthropic directory's policy finding "Image or font
file that the plugin's code could run", cited on the three hook scripts and
plugin.json, and the version "stays held for review". v0.41.0, with no icon, was not
flagged. The hooks' dispatch() loop runs executables from a hooks.d directory only
known at runtime, so the scanner cannot rule out that the icon is one of them.

The listing icon is set only on a plugin's first portal save or submission, which for
remember predates the icon, so shipping one bought nothing but the warning it cleared.
The "No icon" (ICON_MISSING) warning is the expected, harmless price of removing it.

The detector here is the release check's own byte sniff (`check_release_tree.sniff`)
plus the image/font extensions, so a text file named `.svg` counts too. The positive
control drops a PNG into a copy of the same built tree and requires the detector to
name it: without that, an empty walk or a broken sniff would pass "ships no image".
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / ".github" / "scripts"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"

FONT_EXTS = {".woff", ".woff2", ".ttf", ".otf", ".ttc", ".eot"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_no_image", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _images_and_fonts(root: Path) -> list:
    check = _load("check_release_tree")
    found = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        kind = check.sniff(p.read_bytes())
        ext = p.suffix.lower()
        if kind in ("image", "font") or ext in check.IMAGE_EXTS or ext in FONT_EXTS:
            found.append(rel)
    return found


@pytest.fixture(scope="module")
def built_tree(tmp_path_factory) -> Path:
    build = _load("build_release_tree")
    out = tmp_path_factory.mktemp("release") / "out"
    build.build(REPO_ROOT, "HEAD", out, build.load_config(CONFIG))
    return out


def test_the_built_release_tree_ships_no_image_or_font(built_tree):
    assert (built_tree / ".claude-plugin" / "plugin.json").is_file()  # the walk saw a real tree
    assert _images_and_fonts(built_tree) == []


def test_the_detector_names_an_image_dropped_into_the_same_tree(built_tree, tmp_path):
    """Positive control: the walk above would also pass if it found nothing at all."""
    copy = tmp_path / "copy"
    shutil.copytree(built_tree, copy)
    (copy / ".claude-plugin" / "icon.png").write_bytes(PNG)
    (copy / "fonts").mkdir()
    (copy / "fonts" / "x.woff2").write_bytes(b"wOF2" + b"\x00" * 32)
    assert _images_and_fonts(copy) == [".claude-plugin/icon.png", "fonts/x.woff2"]


@pytest.mark.parametrize("where", ["repo", "built"])
def test_plugin_json_names_no_icon(built_tree, where):
    root = REPO_ROOT if where == "repo" else built_tree
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "remember"  # positive control: the manifest was really read
    assert "icon" not in manifest


def test_the_codex_manifest_names_no_icon_either():
    manifest = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "remember"
    assert "icon" not in manifest
