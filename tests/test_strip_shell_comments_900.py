"""#900 -- every shipped .sh loses its comment-only lines at release-build time.

The directory's release-preview scanner reads shell comments as code, the same way it reads
Python comments (test_strip_python_900.py): a `${arr[$key]}` quoted in a comment in
scripts/lib-memory-context.sh was cited as a credential read. The four hooks.json-registered
hooks already lose theirs when compile_hooks.py inlines them; build_release_tree.py now runs
every OTHER shipped .sh through the same quote- and heredoc-aware stripper
(compile_hooks.strip_whole_line_comments) and fails the build if `bash -n` rejects the result.

Every "must be gone" assertion is paired with a "must stay" one -- the shebang, a `#` line
inside a multi-line single-quoted string, a `#` line inside a heredoc, an inline
`cmd # comment` -- and the parse guard is shown failing on a deliberately broken stripper
next to the real stripper passing it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / ".github" / "scripts"
BUILD = SCRIPTS / "build_release_tree.py"
CHECK = SCRIPTS / "check_release_tree.py"
SMOKE = SCRIPTS / "smoke_release_tree.py"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"

posix_only = pytest.mark.skipif(os.name == "nt", reason="hook commands are bash; ps scan is POSIX")


def _load(path: Path, name: str):
    assert path.exists(), f"{path} does not exist (#900)"
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The bash the build itself would use (Git Bash on Windows, never the WSL launcher).
BASH = _load(BUILD, "build_release_tree_sh900_resolve").resolve_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="the build runs bash -n; no usable bash")


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=_git_env()).stdout


# A shipped, non-hook library script. Each shape the stripper must tell apart is here once.
LIB = (
    "#!/usr/bin/env bash\n"
    "# a whole-line comment quoting ${arr[$key]}\n"
    "set -eu\n"
    "    # an indented whole-line comment\n"
    "msg='first line\n"
    "# a hash line inside a single-quoted string\n"
    "last line'\n"
    "cat <<EOF\n"
    "# a hash line inside a heredoc\n"
    "EOF\n"
    "printf '%s\\n' \"$msg\" # an inline comment stays\n"
)

LIB_STRIPPED = (
    "#!/usr/bin/env bash\n"
    "set -eu\n"
    "msg='first line\n"
    "# a hash line inside a single-quoted string\n"
    "last line'\n"
    "cat <<EOF\n"
    "# a hash line inside a heredoc\n"
    "EOF\n"
    "printf '%s\\n' \"$msg\" # an inline comment stays\n"
)

NO_COMMENTS = "#!/bin/sh\necho plain\n"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src"
    files = {
        ".claude-plugin/plugin.json": json.dumps({"name": "example", "version": "0.2.0"}),
        "hooks/hooks.json": json.dumps({"hooks": {}}),
        "scripts/lib-x.sh": LIB,
        "scripts/plain.sh": NO_COMMENTS,
        "hooks.d/after_save/50-x.sh": "#!/bin/sh\n# drop me\necho after\n",
        "LICENSE": "license\n",
        "README.md": "# Example\n",
        "CHANGELOG.md": "# Changelog\n\n## [0.2.0] - 2026-01-02\n\n- fix\n",
    }
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_git_env())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _config() -> dict:
    return {"repo": "Example-Org/example-plugin", "default_branch": "main", "deny": [],
            "changelog": "CHANGELOG.md", "rewrite_links": True}


def _build(tmp_path: Path, mod=None) -> Path:
    mod = mod or _load(BUILD, "build_release_tree_sh900")
    out = tmp_path / "out"
    mod.build(_make_repo(tmp_path), "HEAD", out, _config())
    return out


def _read(out: Path, rel: str) -> str:
    return (out / rel).read_bytes().decode("utf-8")


# -- a fixture repo ---------------------------------------------------------------------

@needs_bash
def test_a_shipped_non_hook_sh_loses_its_comment_lines_and_keeps_its_shebang(tmp_path):
    shipped = _read(_build(tmp_path), "scripts/lib-x.sh")
    assert shipped.startswith("#!/usr/bin/env bash\n"), shipped
    assert "whole-line comment" not in shipped, shipped
    assert "${arr[$key]}" not in shipped, shipped
    # Positive control: the build did not just empty the file.
    assert "set -eu" in shipped, shipped


@needs_bash
def test_a_hash_line_inside_a_quoted_string_or_a_heredoc_is_kept(tmp_path):
    shipped = _read(_build(tmp_path), "scripts/lib-x.sh")
    assert "# a hash line inside a single-quoted string\n" in shipped, shipped
    assert "# a hash line inside a heredoc\n" in shipped, shipped


@needs_bash
def test_an_inline_comment_is_kept_only_whole_lines_go(tmp_path):
    shipped = _read(_build(tmp_path), "scripts/lib-x.sh")
    assert "# an inline comment stays" in shipped, shipped
    assert shipped == LIB_STRIPPED


@needs_bash
def test_every_shipped_sh_is_stripped_not_only_scripts(tmp_path):
    out = _build(tmp_path)
    assert _read(out, "hooks.d/after_save/50-x.sh") == "#!/bin/sh\necho after\n"
    # a file with nothing to strip ships byte-identical
    assert _read(out, "scripts/plain.sh") == NO_COMMENTS


@needs_bash
def test_the_stripped_script_prints_what_the_source_prints(tmp_path):
    out = _build(tmp_path)
    src = tmp_path / "src-lib.sh"
    src.write_bytes(LIB.encode("utf-8"))

    def run(p: Path) -> str:
        return subprocess.run([BASH, str(p)], capture_output=True, text=True,
                              check=True).stdout

    expected = run(src)
    assert "# a hash line inside a heredoc" in expected
    assert run(out / "scripts" / "lib-x.sh") == expected


@needs_bash
def test_a_stripped_sh_that_no_longer_parses_fails_the_build(tmp_path, monkeypatch):
    mod = _load(BUILD, "build_release_tree_sh900_broken")
    monkeypatch.setattr(mod, "strip_whole_line_comments",
                        lambda text: text + "if true; then\n")
    with pytest.raises(mod.BuildError, match=r"\.sh: no longer parses"):
        _build(tmp_path, mod)


# -- which bash proves the result --------------------------------------------------------
# On windows-latest a bare "bash" is commonly the WSL launcher in System32, which
# CreateProcess finds before PATH (tests/_bash_runner.py, #432). The build must parse
# with Git Bash there, or refuse loudly -- never run the WSL stub.

def test_the_build_never_takes_the_wsl_launcher_on_windows(monkeypatch):
    mod = _load(BUILD, "build_release_tree_sh900_win")
    monkeypatch.setattr(mod.sys, "platform", "win32")
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "C:/Windows/System32/bash.exe")
    assert mod.resolve_bash() is None


def test_the_build_takes_git_bash_on_windows(monkeypatch):
    # positive control for the one above
    mod = _load(BUILD, "build_release_tree_sh900_win2")
    monkeypatch.setattr(mod.sys, "platform", "win32")
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        monkeypatch.delenv(var, raising=False)
    git_bash = "C:/Program Files/Git/bin/bash.exe"
    monkeypatch.setattr(mod.shutil, "which", lambda _n: git_bash)
    assert mod.resolve_bash() == git_bash


def test_a_build_with_comments_to_strip_and_no_bash_fails(tmp_path, monkeypatch):
    mod = _load(BUILD, "build_release_tree_sh900_nobash")
    monkeypatch.setattr(mod, "resolve_bash", lambda: None)
    with pytest.raises(mod.BuildError, match="no bash"):
        _build(tmp_path, mod)


# -- the real tree ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def built_tree(tmp_path_factory):
    build = _load(BUILD, "build_release_tree_sh900_real")
    out = tmp_path_factory.mktemp("sh900") / "out"
    build.build(REPO_ROOT, "HEAD", out, build.load_config(CONFIG))
    return out


def _shipped_sh(root: Path) -> list:
    return sorted(p for p in root.rglob("*.sh") if p.is_file())


@needs_bash
def test_no_shipped_sh_in_this_repo_keeps_a_comment_only_line(built_tree):
    compile_hooks = _load(SCRIPTS / "compile_hooks.py", "compile_hooks_sh900")
    files = _shipped_sh(built_tree)
    assert built_tree / "scripts" / "lib-memory-context.sh" in files
    for p in files:
        text = p.read_bytes().decode("utf-8")
        assert compile_hooks.whole_line_comments(text) == [], p
        rel = p.relative_to(built_tree).as_posix()
        src = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"HEAD:{rel}"],
                             capture_output=True, check=True).stdout.decode("utf-8")
        if src.startswith("#!"):
            assert text.split("\n", 1)[0] == src.split("\n", 1)[0], rel


@needs_bash
def test_the_comment_the_scanner_cited_is_gone(built_tree):
    shipped = (built_tree / "scripts" / "lib-memory-context.sh").read_bytes().decode("utf-8")
    source = (REPO_ROOT / "scripts" / "lib-memory-context.sh").read_text(encoding="utf-8")
    assert "${arr[$key]}" in source, "the source lost the comment this test is about"
    assert "${arr[$key]}" not in shipped


@needs_bash
def test_this_repository_built_tree_has_no_check_failures(built_tree):
    check = _load(CHECK, "check_release_tree_sh900")
    budget = json.loads(CONFIG.read_text(encoding="utf-8"))["budget"]
    result = check.check_tree(built_tree, dict(budget))
    assert result.offenders == [], result.offenders


@posix_only
def test_this_repository_built_tree_passes_the_hook_smoke(built_tree):
    smoke = _load(SMOKE, "smoke_release_tree_sh900")
    result = smoke.run_smoke(built_tree, validate="skip", linger_seconds=30)
    assert result.ok, result.report()
