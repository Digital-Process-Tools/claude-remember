#!/usr/bin/env python3
"""Build the slim `release` tree from a git ref (#851).

The Anthropic plugin directory fetches a branch and holds any version whose plugin
folder breaks its file rules (every non-image, non-font file under 256 KiB, at most
512 files, no `.gitattributes` with export-ignore/export-subst/filter). `main` keeps
everything; this script produces the tree a `release` branch carries:

1. Every blob of REF is extracted with `git ls-tree` + `git cat-file --batch`. Not
   `git archive`: that only drops paths through `export-ignore`, which the directory
   refuses to validate at all. The working tree is never read, so an uncommitted
   edit cannot leak into a release.
2. The config's deny-list is dropped. Deny, not allow: a path forgotten here ships
   and is caught loudly by check_release_tree.py; a path forgotten in an allow-list
   would vanish from every user's install with no error anywhere. CHANGELOG.md is on
   that deny-list: only README and LICENSE are required by the directory (#898).
3. `release_readme`, if configured, swaps its own content in for README.md -- a
   short release-only README with no `$VAR`/`${...}` and no network command names,
   while the full README with every disclosure detail stays on the default branch.
4. Relative links and images in every shipped `.md` file that point at a path the
   deny-list removed are rewritten to absolute URLs on the default branch:
   raw.githubusercontent.com for images, github.com/.../blob for everything else.

Usage:
    build_release_tree.py --ref v0.36.0 --out /tmp/release-tree [--repo .]
                          [--config .github/release-branch.json]

The config (see .github/release-branch.json) carries the repo slug, default branch,
deny-list and budget, so another repository can reuse this file unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from check_release_tree import gitattributes_offences
from compile_hooks import HOOK_SCRIPT_NAMES, InlineError, compile_hook
from strip_python import StripError, strip_python

DEFAULT_CONFIG = _HERE.parent / "release-branch.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


class BuildError(Exception):
    """A build that must not produce a tree."""


# -- config -----------------------------------------------------------------------

def load_config(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("repo", "default_branch", "deny"):
        if key not in cfg:
            raise BuildError(f"{path}: missing required key {key!r}")
    return cfg


def is_denied(path: str, deny: list[str]) -> bool:
    for entry in deny:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


# -- git --------------------------------------------------------------------------

def _git_env() -> dict:
    # An outer git (a hook, a worktree command) can export GIT_DIR/GIT_INDEX_FILE;
    # inherited, they would point every call below at the wrong repository.
    return {k: v for k, v in os.environ.items()
            if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                         "GIT_OBJECT_DIRECTORY", "GIT_PREFIX")}


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), *args], input=data,
                       capture_output=True, env=_git_env(), check=False)
    if r.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: "
                         f"{r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def _ls_tree(repo: Path, commit: str) -> list[tuple[str, str, str]]:
    """(mode, sha, path) for every entry of COMMIT, recursively."""
    entries = []
    for rec in _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, _type, sha = meta.decode().split()
        entries.append((mode, sha, path.decode("utf-8")))
    return entries


def _cat_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    raw = _git(repo, "cat-file", "--batch", data=("\n".join(shas) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for _ in shas:
        nl = raw.index(b"\n", pos)
        sha, kind, size = raw[pos:nl].decode().split()
        if kind != "blob":
            raise BuildError(f"{sha}: expected a blob, got {kind}")
        start = nl + 1
        blobs[sha] = raw[start:start + int(size)]
        pos = start + int(size) + 1
    return blobs


# -- CHANGELOG --------------------------------------------------------------------

_SECTION = re.compile(r"^## \[([^\]]+)\]")
_LINKDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S")


def cut_changelog(text: str, full_url: str) -> str:
    """Keep the preamble, the first RELEASED `## [x]` section and its link
    definition, plus a pointer to the full file. `[Unreleased]` is never the
    section kept -- empty or not, it is not what this version shipped."""
    lines = text.splitlines(keepends=True)
    heads = [i for i, line in enumerate(lines) if line.startswith("## ")]
    preamble = lines[:heads[0]] if heads else lines
    for n, i in enumerate(heads):
        m = _SECTION.match(lines[i])
        if not m or m.group(1).strip().lower() == "unreleased":
            continue
        label = m.group(1)
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        section = lines[i:end]
        while section and (not section[-1].strip() or _LINKDEF.match(section[-1])):
            section.pop()
        defn = re.compile(r"^\s{0,3}\[" + re.escape(label) + r"\]:\s*\S")
        linkdef = next((line for line in lines if defn.match(line)), None)
        while preamble and not preamble[-1].strip():
            preamble = preamble[:-1]
        out = "".join(preamble).rstrip("\n") + "\n\n"
        out += ("This file carries only the latest release. The full history is in "
                f"[CHANGELOG.md on the default branch]({full_url}).\n\n")
        out += "".join(section).rstrip("\n") + "\n"
        if linkdef:
            out += "\n" + linkdef.rstrip("\n") + "\n"
        return out
    raise BuildError("CHANGELOG: no released `## [x.y.z]` section found "
                     "(only [Unreleased], or no sections at all)")


# -- link rewriting ---------------------------------------------------------------

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_INLINE = re.compile(r"(\]\(\s*)(<[^>\n]*>|[^)\s]+)")
_REFDEF = re.compile(r"^(\s{0,3}\[[^\]]+\]:[ \t]*)(<[^>\n]*>|\S+)")
_HTML = re.compile(r"(\b(src|href)\s*=\s*)([\"'])(.*?)\3", re.IGNORECASE)
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def _absolute(target: str, md_dir: str, should_rewrite: Callable[[str], str | None],
              repo: str, ref: str, force_raw: bool = False) -> str | None:
    bracketed = target.startswith("<") and target.endswith(">")
    t = target[1:-1] if bracketed else target
    if not t or t.startswith(("#", "//")) or _SCHEME.match(t):
        return None
    frag = ""
    for sep in ("#", "?"):
        if sep in t:
            t, rest = t.split(sep, 1)
            frag = sep + rest + frag
    path = unquote(t)
    rel = path.lstrip("/") if path.startswith("/") else posixpath.join(md_dir, path)
    rel = posixpath.normpath(rel)
    if rel in (".", "") or rel.startswith("../"):
        return None
    kind = should_rewrite(rel)
    if kind is None:
        return None
    if force_raw or posixpath.splitext(rel)[1].lower() in IMAGE_EXTS:
        url = f"https://raw.githubusercontent.com/{repo}/{ref}/{quote(rel)}"
    else:
        url = f"https://github.com/{repo}/{kind}/{ref}/{quote(rel)}"
    url += frag
    return f"<{url}>" if bracketed else url


def rewrite_links(text: str, md_path: str, should_rewrite: Callable[[str], str | None],
                  repo: str, ref: str) -> tuple[str, int]:
    """Rewrite relative markdown/HTML link targets for which SHOULD_REWRITE(path)
    returns "blob" (a removed file) or "tree" (a removed directory). Fenced code
    and inline code spans are left alone."""
    md_dir = posixpath.dirname(md_path)
    count = 0

    def fix(target: str, force_raw: bool = False) -> str:
        nonlocal count
        new = _absolute(target, md_dir, should_rewrite, repo, ref, force_raw)
        if new is None:
            return target
        count += 1
        return new

    def fix_prose(seg: str) -> str:
        seg = _INLINE.sub(lambda m: m.group(1) + fix(m.group(2)), seg)
        return _HTML.sub(lambda m: m.group(1) + m.group(3)
                         + fix(m.group(4), m.group(2).lower() == "src") + m.group(3), seg)

    out = []
    fence = None
    for line in text.splitlines(keepends=True):
        fm = _FENCE.match(line)
        if fence:
            if fm and fm.group(1) == fence:
                fence = None
            out.append(line)
            continue
        if fm:
            fence = fm.group(1)
            out.append(line)
            continue
        m = _REFDEF.match(line)
        if m:
            line = m.group(1) + fix(m.group(2)) + line[m.end():]
            out.append(line)
            continue
        parts = re.split(r"(`+[^`]*`+)", line)
        out.append("".join(p if p.startswith("`") else fix_prose(p) for p in parts))
    return "".join(out), count


# -- build ------------------------------------------------------------------------

def build(repo: Path, ref: str, out: Path, config: dict) -> dict:
    repo, out = Path(repo), Path(out)
    if out.exists() and any(out.iterdir()):
        raise BuildError(f"output directory {out} is not empty -- refusing to write into it")
    try:
        commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
    except BuildError:
        raise BuildError(f"ref {ref!r} does not resolve to a commit in {repo}") from None

    deny = list(config.get("deny", []))
    entries = _ls_tree(repo, commit)
    kept, removed = [], []
    for mode, sha, path in entries:
        if is_denied(path, deny):
            removed.append(path)
            continue
        if mode == "160000":
            raise BuildError(f"{path}: submodules are not supported in a release tree")
        if mode == "120000":
            raise BuildError(f"{path}: symlinks are not supported in a release tree")
        kept.append((mode, sha, path))

    blobs = _cat_blobs(repo, sorted({sha for _, sha, _ in kept}))
    contents = {path: blobs[sha] for _, sha, path in kept}

    release_readme = config.get("release_readme")
    if release_readme:
        if "README.md" not in contents:
            raise BuildError("README.md: missing, cannot swap in release_readme")
        swap_entry = next(((mode, sha) for mode, sha, path in entries
                           if path == release_readme), None)
        if swap_entry is None:
            raise BuildError(f"{release_readme}: configured as release_readme but not "
                             f"found at {ref}")
        swap_mode, swap_sha = swap_entry
        # Same rule the main kept/removed loop above already applies to every other
        # shipped path: a symlink or submodule blob is not something to trust as
        # literal file content (review finding, #898).
        if swap_mode == "120000":
            raise BuildError(f"{release_readme}: symlinks are not supported in a "
                             f"release tree")
        if swap_mode == "160000":
            raise BuildError(f"{release_readme}: submodules are not supported in a "
                             f"release tree")
        swap_blobs = _cat_blobs(repo, [swap_sha])
        contents["README.md"] = swap_blobs[swap_sha]

    # #900: the directory's release-preview validator never follows a
    # `source`/`.` statement out of a hooks.json command into a second file,
    # so each of the four hooks.json-registered scripts ships self-contained
    # -- every file in its own source chain compiled in as one function,
    # each `source` of it turned into a call (so each library's own runtime
    # guard, not a build-time one, decides whether a repeat does anything),
    # with comment-only lines then stripped to fit the 256 KiB per-file
    # budget and unreached functions shaken out. A source line
    # this cannot resolve fails the build rather than shipping a hook that
    # still needs a second file to exist on disk.
    sh_texts = {p: contents[p].decode("utf-8") for p in contents
                if p.startswith("scripts/") and p.endswith(".sh")}
    for hname in HOOK_SCRIPT_NAMES:
        hrel = f"scripts/{hname}"
        if hrel not in contents:
            continue
        try:
            compiled = compile_hook(hrel, sh_texts)
        except InlineError as exc:
            raise BuildError(f"{hrel}: {exc}") from None
        contents[hrel] = compiled.encode("utf-8")

    # #900: the directory's scanner reads Python comments and docstrings as code
    # (release-preview probe hD), so every shipped .py loses both here. strip_python
    # proves each result -- parses as 3.9, same AST as the source minus its
    # docstrings, nothing left -- and a file it cannot prove fails the build.
    py_before = py_after = 0
    for path in sorted(contents):
        if not path.endswith(".py"):
            continue
        try:
            src = contents[path].decode("utf-8")
        except UnicodeDecodeError:
            raise BuildError(f"{path}: not UTF-8, cannot strip it") from None
        try:
            stripped = strip_python(src, path).encode("utf-8")
        except StripError as exc:
            raise BuildError(str(exc)) from None
        py_before += len(contents[path])
        py_after += len(stripped)
        contents[path] = stripped

    for path, data in contents.items():
        if posixpath.basename(path) == ".gitattributes":
            bad = gitattributes_offences(data.decode("utf-8", "replace"))
            if bad:
                raise BuildError(f"{path}: {', '.join(bad)} -- the directory stops "
                                 "validating on these attributes")

    slug = config["repo"]
    branch = config.get("link_ref") or config["default_branch"]
    changelog = config.get("changelog")
    if changelog and changelog in contents:
        url = f"https://github.com/{slug}/blob/{config['default_branch']}/{changelog}"
        contents[changelog] = cut_changelog(contents[changelog].decode("utf-8"), url).encode("utf-8")

    rewritten: dict[str, int] = {}
    if config.get("rewrite_links", True):
        removed_set = set(removed)
        removed_dirs = {posixpath.dirname(p) for p in removed}
        for p in list(removed_dirs):
            while p:
                p = posixpath.dirname(p)
                removed_dirs.add(p)
        kept_paths = set(contents)
        kept_dirs = set()
        for p in kept_paths:
            d = posixpath.dirname(p)
            while d:
                kept_dirs.add(d)
                d = posixpath.dirname(d)

        def should_rewrite(rel: str) -> str | None:
            if rel in removed_set:
                return "blob"
            if rel in removed_dirs and rel not in kept_dirs and rel not in kept_paths:
                return "tree"
            return None

        for path in sorted(contents):
            if not path.lower().endswith(".md"):
                continue
            new, n = rewrite_links(contents[path].decode("utf-8"), path, should_rewrite, slug, branch)
            if n:
                contents[path] = new.encode("utf-8")
                rewritten[path] = n

    out.mkdir(parents=True, exist_ok=True)
    for mode, _sha, path in kept:
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(contents[path])
        if os.name != "nt":
            os.chmod(dest, 0o755 if mode == "100755" else 0o644)

    unused = [e for e in deny if not any(is_denied(p, [e]) for _, _, p in entries)]
    return {"ref": ref, "commit": commit, "kept": len(kept), "removed": removed,
            "rewritten": rewritten, "unused_deny": unused,
            "py_bytes": (py_before, py_after)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".", help="git repository to read REF from")
    ap.add_argument("--ref", required=True, help="tag, branch or sha to build")
    ap.add_argument("--out", required=True, help="output directory (absent or empty)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--repo-slug", help="owner/name for absolute links (overrides config)")
    ap.add_argument("--default-branch", help="branch absolute links point at (overrides config)")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
        if args.repo_slug:
            cfg["repo"] = args.repo_slug
        if args.default_branch:
            cfg["default_branch"] = args.default_branch
        report = build(Path(args.repo), args.ref, Path(args.out), cfg)
    except BuildError as exc:
        print(f"build_release_tree: {exc}", file=sys.stderr)
        return 1
    print(f"built {report['ref']} ({report['commit']}): {report['kept']} files kept, "
          f"{len(report['removed'])} removed by the deny-list")
    before, after = report["py_bytes"]
    print(f"  stripped comments and docstrings from shipped .py: {before} -> {after} bytes")
    for path, n in sorted(report["rewritten"].items()):
        print(f"  rewrote {n} link(s) in {path}")
    for entry in report["unused_deny"]:
        print(f"  note: deny entry {entry!r} matched nothing at this ref")
    return 0


if __name__ == "__main__":
    sys.exit(main())
