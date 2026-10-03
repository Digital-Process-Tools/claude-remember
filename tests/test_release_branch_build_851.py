"""#851 -- the build script that turns a git ref into the slim `release` tree.

The Anthropic directory holds any plugin folder with a non-image file of 256 KiB or
more, more than 512 files, or a `.gitattributes` carrying export-ignore/export-subst/
filter. `main` keeps everything; `.github/scripts/build_release_tree.py` extracts a
ref through `git ls-tree` + `git cat-file` (never `git archive`, which would need
export-ignore), drops a deny-list, cuts CHANGELOG.md to the latest released section,
and rewrites links that would otherwise point at removed paths.

Written before the script existed. Every "must be removed" assertion is paired with
a "must be kept" one: a build that produced an empty tree would pass every removal
check, and a build that copied everything would pass every keep check.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"

SLUG = "Example-Org/example-plugin"
RAW = "https://raw.githubusercontent.com/Example-Org/example-plugin/main/"
BLOB = "https://github.com/Example-Org/example-plugin/blob/main/"


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#851)"
    spec = importlib.util.spec_from_file_location("build_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=_git_env(),
    ).stdout


CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- something not released yet (must not ship)

## [0.2.0] - 2026-01-02 -- second

### Fixed

- the latest released fix

## [0.1.0] - 2026-01-01 -- first

- the oldest entry (must not ship)

[Unreleased]: https://github.com/Example-Org/example-plugin/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.2.0
[0.1.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.1.0
"""

README = """# Example

![logo](docs/logo.png)

<p align="center"><img src="docs/logo.png" width="200"></p>

[![License](https://img.shields.io/badge/x-y-z)](LICENSE)

Read [the guide](docs/guide.md#install) or [the changelog](CHANGELOG.md).

Absolute stays: [site](https://example.com/docs/guide.md).

Anchor stays: [below](#example).

```
![in a fence](docs/logo.png)
```

Inline code stays: `[x](docs/guide.md)`.

[ref-style]: docs/guide.md
"""

SKILL = """---
name: s
description: d
---

See [the guide](../../docs/guide.md) and [hooks](../../hooks/hooks.json).
"""


def _make_repo(tmp_path: Path, *, changelog: str = CHANGELOG,
               extra: dict | None = None) -> Path:
    repo = tmp_path / "src"
    files = {
        ".claude-plugin/plugin.json": json.dumps({"name": "example", "version": "0.2.0"}),
        "hooks/hooks.json": json.dumps({"hooks": {}}),
        "scripts/run.sh": "#!/bin/sh\necho hi\n",
        "scripts/run-tests.sh": "#!/bin/sh\npytest\n",
        "pipeline/__init__.py": "",
        "skills/s/SKILL.md": SKILL,
        "tests/test_x.py": "def test_x():\n    pass\n",
        "docs/guide.md": "# Guide\n",
        "docs/logo.png": "\x89PNG fake\n",
        ".github/workflows/x.yml": "name: x\n",
        ".claude/jit-context/rule.md": "rule\n",
        "CLAUDE.md": "# dev notes\n",
        "LICENSE": "license\n",
        "README.md": README,
        "CHANGELOG.md": changelog,
    }
    files.update(extra or {})
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_git_env())
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x", "scripts/run.sh")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v0.2.0")
    return repo


def _config(**over) -> dict:
    cfg = {
        "repo": SLUG,
        "default_branch": "main",
        "deny": ["tests/", "docs/", ".github/", ".claude/", "CLAUDE.md",
                 "scripts/run-tests.sh"],
        "changelog": "CHANGELOG.md",
        "rewrite_links": True,
    }
    cfg.update(over)
    return cfg


def _build(tmp_path: Path, repo: Path, cfg: dict | None = None, ref: str = "v0.2.0") -> Path:
    mod = _load()
    out = tmp_path / "out"
    mod.build(repo, ref, out, cfg or _config())
    return out


def _files(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# -- deny-list ------------------------------------------------------------------

def test_denied_paths_are_removed_and_everything_else_is_kept(tmp_path):
    out = _build(tmp_path, _make_repo(tmp_path))
    files = _files(out)
    for gone in ("tests/test_x.py", "docs/guide.md", "docs/logo.png",
                 ".github/workflows/x.yml", ".claude/jit-context/rule.md",
                 "CLAUDE.md", "scripts/run-tests.sh"):
        assert gone not in files, f"{gone} is on the deny-list but shipped"
    # Positive control: the build did not just produce an empty tree.
    for kept in (".claude-plugin/plugin.json", "hooks/hooks.json", "scripts/run.sh",
                 "pipeline/__init__.py", "skills/s/SKILL.md", "LICENSE",
                 "README.md", "CHANGELOG.md"):
        assert kept in files, f"{kept} is not on the deny-list but was dropped"


def test_a_file_entry_is_exact_and_a_directory_entry_needs_its_slash(tmp_path):
    """`CLAUDE.md` must not take `docs2/CLAUDE.md` or `CLAUDE.md.bak` with it, and
    `tests/` must not take `tests-helper.sh`."""
    repo = _make_repo(tmp_path, extra={
        "sub/CLAUDE.md": "kept\n", "CLAUDE.md.bak": "kept\n", "tests-helper.sh": "kept\n",
    })
    files = _files(_build(tmp_path, repo))
    assert "CLAUDE.md" not in files
    assert {"sub/CLAUDE.md", "CLAUDE.md.bak", "tests-helper.sh"} <= files


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_executable_bit_survives_extraction(tmp_path):
    out = _build(tmp_path, _make_repo(tmp_path))
    assert os.stat(out / "scripts/run.sh").st_mode & stat.S_IXUSR
    # Positive control: a 100644 blob is not made executable wholesale.
    assert not os.stat(out / "LICENSE").st_mode & stat.S_IXUSR


def test_the_ref_is_built_not_the_working_tree(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "LICENSE").write_text("uncommitted edit\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    out = _build(tmp_path, repo)
    assert (out / "LICENSE").read_text(encoding="utf-8") == "license\n"
    assert not (out / "untracked.txt").exists()


def test_a_non_empty_output_directory_is_refused(tmp_path):
    mod = _load()
    repo = _make_repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "precious.txt").write_text("x", encoding="utf-8")
    with pytest.raises(mod.BuildError, match="not empty"):
        mod.build(repo, "v0.2.0", out, _config())
    assert (out / "precious.txt").exists()
    # Positive control: an existing EMPTY directory is accepted.
    out2 = tmp_path / "out2"
    out2.mkdir()
    mod.build(repo, "v0.2.0", out2, _config())
    assert (out2 / "LICENSE").exists()


def test_a_forbidden_gitattributes_stops_the_build(tmp_path):
    mod = _load()
    repo = _make_repo(tmp_path, extra={".gitattributes": "tests/ export-ignore\n"})
    with pytest.raises(mod.BuildError, match="export-ignore"):
        mod.build(repo, "v0.2.0", tmp_path / "out", _config())


def test_a_benign_gitattributes_ships(tmp_path):
    repo = _make_repo(tmp_path, extra={".gitattributes": "*.sh text eol=lf\n"})
    out = _build(tmp_path, repo)
    assert (out / ".gitattributes").read_text(encoding="utf-8") == "*.sh text eol=lf\n"


# -- release_readme swap (#898) ---------------------------------------------------

RELEASE_README = "# Release-only README\n\n" + " ".join(["word"] * 40) + "\n"


def test_release_readme_is_swapped_in_for_readme_md(tmp_path):
    repo = _make_repo(tmp_path, extra={"README.release.md": RELEASE_README})
    out = _build(tmp_path, repo, _config(release_readme="README.release.md",
                                          deny=["tests/", "docs/", ".github/", ".claude/",
                                                "CLAUDE.md", "scripts/run-tests.sh",
                                                "README.release.md"]))
    assert (out / "README.md").read_text(encoding="utf-8") == RELEASE_README
    # Positive control: the swap source itself is not shipped under its own name.
    assert not (out / "README.release.md").exists()


def test_release_readme_not_found_at_ref_is_a_build_error(tmp_path):
    mod = _load()
    repo = _make_repo(tmp_path)  # no README.release.md committed
    with pytest.raises(mod.BuildError, match="release_readme"):
        mod.build(repo, "v0.2.0", tmp_path / "out",
                  _config(release_readme="README.release.md"))


def test_release_readme_as_a_symlink_is_refused(tmp_path):
    mod = _load()
    repo = _make_repo(tmp_path)
    (repo / "README.release.md").symlink_to(repo / "README.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add symlink release readme")
    _git(repo, "tag", "-f", "v0.2.0")
    with pytest.raises(mod.BuildError, match="symlink"):
        mod.build(repo, "v0.2.0", tmp_path / "out",
                  _config(release_readme="README.release.md"))


def test_no_release_readme_configured_leaves_readme_untouched(tmp_path):
    """Positive control: without `release_readme` set, README.md ships as committed."""
    out = _build(tmp_path, _make_repo(tmp_path))
    text = (out / "README.md").read_text(encoding="utf-8")
    assert "Read" in text and "the guide" in text


# -- CHANGELOG -------------------------------------------------------------------

def test_changelog_keeps_only_the_latest_released_section(tmp_path):
    text = (_build(tmp_path, _make_repo(tmp_path)) / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.2.0] - 2026-01-02" in text
    assert "the latest released fix" in text
    assert "[0.2.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.2.0" in text
    assert "## [Unreleased]" not in text
    assert "not released yet" not in text
    assert "[Unreleased]:" not in text
    assert "## [0.1.0]" not in text
    assert "the oldest entry" not in text
    assert "[0.1.0]:" not in text
    assert BLOB + "CHANGELOG.md" in text


def test_changelog_skips_an_empty_unreleased_section_too(tmp_path):
    """The real CHANGELOG's first `## [` is an EMPTY [Unreleased]; "keep the first
    section" would then ship nothing."""
    empty = CHANGELOG.replace(
        "### Added\n\n- something not released yet (must not ship)\n\n", "")
    mod = _load()
    out = mod.cut_changelog(empty, BLOB + "CHANGELOG.md")
    assert "the latest released fix" in out
    assert "## [Unreleased]" not in out


def test_changelog_with_no_released_section_is_an_error_not_an_empty_file():
    mod = _load()
    with pytest.raises(mod.BuildError, match="released"):
        mod.cut_changelog("# Changelog\n\n## [Unreleased]\n\n- x\n", BLOB + "CHANGELOG.md")


def test_changelog_preamble_is_kept():
    mod = _load()
    out = mod.cut_changelog(CHANGELOG, BLOB + "CHANGELOG.md")
    assert out.startswith("# Changelog\n")


# -- link rewriting --------------------------------------------------------------

def test_readme_links_into_removed_paths_become_absolute(tmp_path):
    text = (_build(tmp_path, _make_repo(tmp_path)) / "README.md").read_text(encoding="utf-8")
    assert f"![logo]({RAW}docs/logo.png)" in text
    assert f'<img src="{RAW}docs/logo.png" width="200">' in text
    assert f"[the guide]({BLOB}docs/guide.md#install)" in text
    assert f"[ref-style]: {BLOB}docs/guide.md" in text
    # Only the two inside code (the fence and the inline span) may remain relative.
    assert text.count("](docs/") == 2, text


def test_readme_links_that_still_resolve_are_left_alone(tmp_path):
    text = (_build(tmp_path, _make_repo(tmp_path)) / "README.md").read_text(encoding="utf-8")
    assert "](LICENSE)" in text
    assert "[the changelog](CHANGELOG.md)" in text
    assert "[site](https://example.com/docs/guide.md)" in text
    assert "[below](#example)" in text
    assert "![in a fence](docs/logo.png)" in text, "fenced code must not be rewritten"
    assert "`[x](docs/guide.md)`" in text, "inline code must not be rewritten"


def test_nested_markdown_links_resolve_relative_to_their_own_file(tmp_path):
    text = (_build(tmp_path, _make_repo(tmp_path)) / "skills/s/SKILL.md").read_text(encoding="utf-8")
    assert f"[the guide]({BLOB}docs/guide.md)" in text
    # Positive control: a relative link to a path that ships is untouched.
    assert "[hooks](../../hooks/hooks.json)" in text


def test_the_link_base_comes_from_the_config(tmp_path):
    cfg = _config(repo="Other/thing", default_branch="trunk")
    text = (_build(tmp_path, _make_repo(tmp_path), cfg) / "README.md").read_text(encoding="utf-8")
    assert "https://raw.githubusercontent.com/Other/thing/trunk/docs/logo.png" in text
    assert "https://github.com/Other/thing/blob/trunk/docs/guide.md#install" in text


# -- the real repository ---------------------------------------------------------

def test_the_committed_config_parses_and_names_the_brief_deny_list():
    mod = _load()
    cfg = mod.load_config(CONFIG)
    for entry in ("tests/", "docs/", ".oss/", ".github/", ".claude/", "CLAUDE.md",
                  "CONTRIBUTING.md", "conftest.py", "scripts/run-tests.sh",
                  "scripts/windows_skip_triage_497.py",
                  # #898: CHANGELOG.md is not shipped at all; SECURITY.md and
                  # CODE_OF_CONDUCT.md are not required by the directory and
                  # nothing the plugin runs reads them; README.release.md is
                  # consumed by the swap below, not shipped under its own name.
                  "CHANGELOG.md", "SECURITY.md", "CODE_OF_CONDUCT.md",
                  "README.release.md"):
        assert entry in cfg["deny"], entry
    assert cfg["release_readme"] == "README.release.md"
    # Positive control: nothing the plugin runs is denied.
    for runtime in ("hooks/", "scripts/", "pipeline/", "skills/", "commands/",
                    ".claude-plugin/", "prompts/", "hooks.d/"):
        assert runtime not in cfg["deny"], runtime
    assert cfg["budget"] == {"max_file_bytes": 262144, "max_files": 512,
                             "max_total_bytes": 3 * 1024 * 1024}


def test_building_this_repository_head_ships_every_hook_script(tmp_path):
    """Integration: the real deny-list against the real tree. Every script
    hooks/hooks.json names must survive the build."""
    mod = _load()
    out = tmp_path / "out"
    mod.build(REPO_ROOT, "HEAD", out, mod.load_config(CONFIG))
    hooks = json.loads((out / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for groups in hooks["hooks"].values()
                for g in groups for h in g["hooks"]]
    assert commands
    for cmd in commands:
        rel = cmd.split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].split('"', 1)[0]
        assert (out / rel).is_file(), f"{rel} (named by hooks.json) did not ship"
    assert not (out / "tests").exists()
    assert not (out / "docs").exists()
    # #898: CHANGELOG.md is not shipped at all, and README.md is the swapped-in
    # release_readme content, not the full README that lives on main.
    assert not (out / "CHANGELOG.md").exists()
    assert not (out / "README.release.md").exists()
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "$" not in readme, "the release README must carry no $VAR/${...}"


def test_cli_builds_and_reports(tmp_path):
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(_config()), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--ref", "v0.2.0",
         "--out", str(out), "--config", str(cfg)],
        capture_output=True, text=True, env=_git_env(), check=False,
    )
    assert r.returncode == 0, r.stderr
    assert (out / "LICENSE").exists()
    assert "removed" in r.stdout


def test_cli_fails_loudly_on_an_unknown_ref(tmp_path):
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps(_config()), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--ref", "v9.9.9",
         "--out", str(tmp_path / "out"), "--config", str(cfg)],
        capture_output=True, text=True, env=_git_env(), check=False,
    )
    assert r.returncode != 0
    assert "v9.9.9" in r.stderr
