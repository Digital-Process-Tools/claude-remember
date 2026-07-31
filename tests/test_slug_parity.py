"""The session-directory slug must agree everywhere it exists (#157/#158/#174).

Three implementations had drifted apart: the bash one in detect-tools.sh (fixed
by #156), a naive inline copy in lib-memory-dir.sh that user-prompt-hook.sh
actually ran, and the Python one in extract.py. None of them truncated at 200
characters the way Claude Code does.

Every disagreement here has the same consequence: the plugin computes a
directory Claude Code never created, finds no transcript, and saves nothing —
in silence. So these tests compare all of them against one oracle rather than
against each other.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.slug import session_dir_slug as py_slug

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess assertions — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_SLUG_SH = REPO_ROOT / "scripts" / "lib-slug.sh"
LIB_MEMORY_DIR_SH = REPO_ROOT / "scripts" / "lib-memory-dir.sh"


def js_slug(path: str) -> str:
    """An independent transcription of the shipped CLI's `JXA`.

        var jXA = 200;
        function JXA(A) {
          let q = A.replace(/[^a-zA-Z0-9]/g, "-");
          if (q.length <= jXA) return q;
          let K = 0;
          for (let _ = 0; _ < A.length; _++)
            K = (K << 5) - K + A.charCodeAt(_), K |= 0;
          return `${q.slice(0, jXA)}-${Math.abs(K).toString(36)}`;
        }

    Written from the JS rather than imported from pipeline.slug, so a bug in
    the implementation cannot quietly become the definition of correct.
    """
    units = path.encode("utf-16-le", "surrogatepass")
    code_units = [units[i] | (units[i + 1] << 8) for i in range(0, len(units), 2)]

    slug = "".join(
        chr(u) if chr(u).isascii() and chr(u).isalnum() else "-" for u in code_units
    )
    if len(slug) <= 200:
        return slug

    acc = 0
    for unit in code_units:
        acc = (acc << 5) - acc + unit
        acc &= 0xFFFFFFFF
        if acc >= 0x80000000:
            acc -= 0x100000000
    magnitude = abs(acc)

    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if magnitude == 0:
        tail = "0"
    else:
        out = []
        while magnitude:
            magnitude, d = divmod(magnitude, 36)
            out.append(digits[d])
        tail = "".join(reversed(out))
    return slug[:200] + "-" + tail


def bash_slug(path: str, lib: Path = LIB_SLUG_SH) -> str:
    """What the shell implementation produces for `path`."""
    script = f"""
    PIPELINE_DIR="{REPO_ROOT}"
    source "{lib}"
    session_dir_slug "$1"
    """
    result = subprocess.run(
        ["bash", "-c", script, "bash", path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


# Deep enough to cross 200 characters without anything exotic — this is a
# monorepo module path under a long home directory, which is how #157 was hit.
DEEP = "/Users/a-developer-with-a-long-name/Documents/clients/acme-holdings/" + "/".join(
    f"packages/module-{i}" for i in range(12)
)

PATHS = [
    "/Users/f/Documents/dvsi",
    "/home/u/p",
    "/tmp/café/projet",
    "/tmp/日本語/プロジェクト",
    "/tmp/emoji-🎉-dir",
    "/tmp/𠮷野家/x",
    "/tmp/mixed-é-🎉-日-𠮷/deep",
    DEEP,
    "/x" * 150,
    "a" * 199,
    "a" * 200,
    "a" * 201,
    "/tmp/" + "🎉" * 120,
]


@pytest.mark.parametrize("path", PATHS)
def test_python_matches_the_oracle(path):
    assert py_slug(path) == js_slug(path)


@pytest.mark.parametrize("path", PATHS)
def test_bash_matches_the_oracle(path):
    assert bash_slug(path) == js_slug(path)


@pytest.mark.parametrize("path", PATHS)
def test_bash_and_python_agree(path):
    """Belt and braces: the two sides the plugin actually runs."""
    assert bash_slug(path) == py_slug(path)


def test_a_deep_path_is_truncated_with_a_hash():
    """#157: nothing truncated, so a deep project silently never saved."""
    slug = py_slug(DEEP)
    assert len(slug) > 200, "the fixture stopped being long enough to test this"
    head, _, tail = slug.rpartition("-")
    assert len(head) == 200, "the first 200 characters must be kept verbatim"
    assert tail and all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in tail)
    assert slug == bash_slug(DEEP)


def test_an_astral_character_costs_two_dashes():
    """#174: the regex has no /u flag, so a surrogate pair is two code units."""
    assert py_slug("/tmp/x🎉y") == "-tmp-x--y"
    assert bash_slug("/tmp/x🎉y") == "-tmp-x--y"


def test_a_bmp_character_costs_one_dash():
    """The other half of the same rule — regressing this breaks CJK paths."""
    assert py_slug("/tmp/日x") == "-tmp--x"
    assert bash_slug("/tmp/日x") == "-tmp--x"


# ── Drive-letter fold parity (#268) ─────────────────────────────────────────
#
# scripts/lib-slug.sh folds a leading Windows drive letter to lower case
# (#263); pipeline/slug.py did not fold at all — it is a faithful
# transcription of Claude Code's own JXA routine, which never sees a raw
# drive letter, so a transcription of it has nothing to fold either. On
# Windows, CLAUDE_PROJECT_DIR reaches the Python side already normalised to
# the native Win32 form with an UPPER-case drive (resolve-paths.sh:174), and
# pipeline.slug slugged that literally: `C--Users-...` from Python against
# `c--Users-...` from bash, for the same directory. NTFS resolves both, so
# nothing failed and nothing reported it.
#
# The JS oracle cannot pin this step: the fold happens to the input BEFORE
# Claude Code's own regex ever runs, so js_slug() has no concept of a drive
# letter at all. These compare bash and python directly instead, across both
# cases and all three shapes CLAUDE_PROJECT_DIR is known to arrive in, so the
# guarantee spans the normalisation step rather than a list of remembered
# inputs — extending test_bash_and_python_agree the same way it already
# covers everything else.
DRIVE_PATHS = [
    r"C:\Users\dev\project",
    r"c:\Users\dev\project",
    "C:/Users/dev/project",
    "c:/Users/dev/project",
    "/c/Users/dev/project",
    "/C/Users/dev/project",
    r"D:\Data\x",
    "C:/",
]


@pytest.mark.parametrize("path", DRIVE_PATHS)
def test_bash_and_python_agree_on_drive_letter_paths(path):
    """#268: bash already folds the drive letter; python must agree."""
    assert bash_slug(path) == py_slug(path)


@pytest.mark.parametrize("path", [r"C:\Users\dev\project", "C:/Users/dev/project"])
def test_the_drive_letter_folds_to_lower_case(path):
    """The direction is pinned explicitly, not just cross-checked (#263's PR):
    cygpath already lower-cases, so the working majority's on-disk stores are
    already spelled that way — uppercasing would "fix" the minority and
    rename every other store. A bare bash==python equality would still pass
    if both sides folded to upper case instead."""
    assert py_slug(path).startswith("c--"), py_slug(path)
    assert bash_slug(path).startswith("c--"), bash_slug(path)


def test_a_drive_letter_not_at_the_start_is_left_alone():
    """The fold is anchored to position 0. An embedded 'C:' elsewhere in a
    path is an ordinary colon to both implementations, not a drive letter —
    folding it would be scope creep past what #263 established."""
    path = "/tmp/weird:C:embedded"
    expected = "-tmp-weird-C-embedded"
    assert py_slug(path) == expected
    assert bash_slug(path) == expected


def test_external_storage_dir_uses_the_real_slug_without_detect_tools():
    """#158: lib-memory-dir.sh carried a naive copy of its own.

    user-prompt-hook.sh reaches it without sourcing detect-tools.sh, so in
    external-storage mode ({slug} in data_dir) that hook resolved a DIFFERENT
    REMEMBER_DIR than every other one — split-brain memory rather than a clean
    failure, and harder to notice than a silent no-op. This drives the real
    resolution path, not the helper.
    """
    path = "/tmp/mixed-é-🎉-日-𠮷/deep"
    script = f"""
    PROJECT_DIR="{REPO_ROOT}"
    PIPELINE_DIR="{REPO_ROOT}"
    source "{LIB_MEMORY_DIR_SH}"
    _resolve_remember_dir "/ext/{{slug}}" "$1"
    """
    result = subprocess.run(
        ["bash", "-c", script, "bash", path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == "/ext/" + js_slug(path)


def test_detect_tools_slug_truncates():
    """The entry point every hook but one actually calls (#157)."""
    script = f"""
    PIPELINE_DIR="{REPO_ROOT}"
    source "{REPO_ROOT / "scripts" / "detect-tools.sh"}"
    session_dir_slug "$1"
    """
    result = subprocess.run(
        ["bash", "-c", script, "bash", DEEP],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == js_slug(DEEP)


@pytest.mark.parametrize("path", ["/tmp/x🎉y", "/tmp/𠮷野家/x", DEEP])
def test_extract_session_dir_matches_the_oracle(path, monkeypatch):
    """The Python entry point that was live-broken in 0.8.7 (#174/#157)."""
    from pipeline.extract import _session_dir

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    assert _session_dir(path) == "/cfg/projects/" + js_slug(path)


def test_the_long_path_hash_survives_without_python():
    """Degrade to the old behaviour rather than to a crash.

    The hash needs a subprocess. If it cannot run, the slug is wrong — but it
    is wrong exactly as it was before this change, and the missing-session-
    directory warning (#156) reports it instead of failing silently.
    """
    script = f"""
    PIPELINE_DIR="/nonexistent"
    PYTHON="/nonexistent/python"
    source "{LIB_SLUG_SH}"
    session_dir_slug "$1"
    """
    result = subprocess.run(
        ["bash", "-c", script, "bash", DEEP],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    untruncated = "".join(
        c if c.isascii() and c.isalnum() else "-" * (2 if ord(c) > 0xFFFF else 1)
        for c in DEEP
    )
    assert result.stdout.rstrip("\n") == untruncated, (
        "with no hash available the untruncated slug is the honest fallback — "
        "wrong in exactly the way it was before, not a crash and not a "
        "truncation with no hash on the end, which would match nothing at all"
    )


# Ill-formed UTF-8: legal on Linux, where filenames are bytes. The real decoder
# folds each ill-formed sequence into ONE replacement character by the
# maximal-subpart rule; the sed byte table gives one dash per byte, which is the
# divergence #186 was opened for.
MALFORMED = [
    b"ab\xe0\xa0cd",          # 3-byte lead, one continuation, then ASCII
    b"x\xf0\x90y",            # 4-byte lead truncated mid-sequence
    b"/tmp/\xff\xfe/x",       # bytes that begin nothing at all
    b"/tmp/caf\xe9",          # Latin-1 "café" — the classic legacy filename
]


@pytest.mark.parametrize("raw", MALFORMED)
def test_malformed_utf8_matches_the_decoder(raw):
    """#186: the shell delegates these to the decoder rather than guessing.

    `errors="replace"` is Python's implementation of the same maximal-subpart
    rule the platform decoder uses, so it stands in for the oracle here.
    """
    decoded = raw.decode("utf-8", "replace")
    expected = js_slug(decoded)

    script = f"""
    PIPELINE_DIR="{REPO_ROOT}"
    REMEMBER_UTF8_STRICT=1
    source "{LIB_SLUG_SH}"
    session_dir_slug "$1"
    """
    result = subprocess.run(
        ["bash", "-c", script, "bash", raw.decode("utf-8", "surrogateescape")],
        capture_output=True, text=True, errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == expected, (
        f"{raw!r}: shell gave {result.stdout.rstrip()!r}, the decoder says "
        f"{expected!r} — one dash per bad byte instead of one per ill-formed "
        "sequence"
    )


def _forks_while_slugging(path: str, tmp_path: Path, *, strict: bool,
                          locale: str | None = None) -> set[str]:
    """Which of `iconv` / `python3` a slug of `path` actually spawns.

    Both are shadowed by markers on PATH, so this counts real forks rather than
    the one the test author happened to think of — the first version of this
    watched only for Python and would have passed while `iconv` forked on every
    non-ASCII path.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in ("iconv", "python3"):
        marker = tmp_path / f"{name}-was-called"
        if marker.exists():
            marker.unlink()
        # Record AND delegate to the real binary. A stub that merely fails
        # changes the behaviour it is measuring: an `iconv` that always exits 1
        # makes every path look ill-formed, which is how the first version of
        # this helper "found" a well-formed path reaching the decoder.
        real = shutil.which(name)
        stub = bindir / name
        stub.write_text(
            f"#!/usr/bin/env bash\ntouch '{marker}'\nexec '{real}' \"$@\"\n"
            if real else f"#!/usr/bin/env bash\ntouch '{marker}'\nexit 127\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    env["PIPELINE_DIR"] = str(REPO_ROOT)
    env["REMEMBER_UTF8_STRICT"] = "1" if strict else "0"
    if locale is not None:
        env["LC_ALL"] = locale
    subprocess.run(
        ["bash", "-c",
         f'source "{LIB_SLUG_SH}"; session_dir_slug "$1" >/dev/null', "bash", path],
        env=env, capture_output=True, text=True,
    )
    return {n for n in ("iconv", "python3") if (tmp_path / f"{n}-was-called").exists()}


def test_an_ascii_path_forks_nothing(tmp_path):
    """session_dir_slug runs on every tool call. The common path must stay in
    the byte table even where the check is enabled."""
    assert _forks_while_slugging("/tmp/plain-ascii", tmp_path, strict=True) == set()


@pytest.mark.parametrize("locale", ["C", "POSIX", "en_US.UTF-8", "C.UTF-8"])
def test_an_ascii_path_forks_nothing_in_any_locale(locale, tmp_path):
    """An ASCII path must never fork, whatever the locale.

    This is defense-in-depth, not a reproduction. The real failure happened on
    the macOS CI runners — three runs red, and green the moment the detection
    was forced to byte semantics with LC_ALL=C — but a sweep of every locale
    installed on the development machine could not reproduce it under bash
    3.2, so the mechanism is unconfirmed and these four locales are not known
    to be the ones that differ.

    Kept because the invariant is worth stating and cheap to check, and
    because CI is where it actually bites. Do not read a pass here as proof
    the underlying bug is gone.
    """
    assert _forks_while_slugging(
        "/tmp/plain-ascii", tmp_path, strict=True, locale=locale
    ) == set()


def test_a_valid_non_ascii_path_pays_at_most_the_check(tmp_path):
    """Accented and CJK paths are ordinary. They may reach `iconv`, which says
    they are well-formed — but they must never reach the decoder."""
    forked = _forks_while_slugging("/tmp/café/日本語/🎉", tmp_path, strict=True)
    assert "python3" not in forked, (
        "a well-formed path was handed to the decoder — that is a subprocess "
        "per tool call bought for nothing"
    )


def test_nothing_forks_at_all_where_the_bug_cannot_happen(tmp_path):
    """macOS enforces well-formed UTF-8 and Windows paths come from UTF-16, so
    neither can produce the input this handles and neither should pay ~6ms per
    tool call to find that out."""
    if sys.platform.startswith("linux"):
        pytest.skip("Linux is where the check is meant to run")
    assert _forks_while_slugging("/tmp/café/日本語/🎉", tmp_path, strict=False) == set()


def test_a_hash_that_is_not_base36_is_refused(tmp_path):
    """The hash arrives from another file resolved through PIPELINE_DIR.

    A stale or wrong-version plugin copy could print anything, and appending it
    verbatim would invent a NEW wrong directory rather than fall back to the
    old wrong one. Base36 is the entire alphabet a real hash can use.
    """
    fake = tmp_path / "pipeline"
    fake.mkdir()
    (fake / "slug.py").write_text("print('WRONG/HASH!')\n", encoding="utf-8")

    script = f"""
    PIPELINE_DIR="{tmp_path}"
    source "{LIB_SLUG_SH}"
    session_dir_slug "$1"
    """
    result = subprocess.run(
        ["bash", "-c", script, "bash", DEEP],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.rstrip("\n")
    assert "WRONG" not in out, f"a non-base36 hash was appended verbatim: {out!r}"
    assert len(out) > 200, "expected the untruncated fallback, not a bare truncation"


def test_home_is_preferred_over_expanduser(monkeypatch):
    """`os.environ["HOME"]` first, `expanduser("~")` only as a fallback.

    On POSIX these agree — `expanduser` reads $HOME — so swapping the order
    changes nothing any platform CI runs on can observe, and the mutation
    survived a green suite (#175). The ordering exists for Windows, where
    `expanduser` prefers USERPROFILE and ignores HOME, so a fixture that sets
    only HOME would be silently ignored. Forcing the two apart is the only way
    to assert which one wins.
    """
    from pipeline.extract import _session_dir

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", "/from-home")
    monkeypatch.setattr("pipeline.extract.os.path.expanduser", lambda p: "/from-expanduser")

    assert _session_dir("/p").startswith("/from-home/.claude/projects/"), (
        "expanduser won over HOME — a test fixture setting only HOME would be "
        "ignored on Windows, which is the case this ordering is for"
    )


def test_expanduser_is_used_when_home_is_unset(monkeypatch):
    """The other half: without HOME, the fallback must still resolve."""
    from pipeline.extract import _session_dir

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr("pipeline.extract.os.path.expanduser", lambda p: "/from-expanduser")

    assert _session_dir("/p").startswith("/from-expanduser/.claude/projects/")


def test_a_newline_in_the_path_still_becomes_a_dash():
    """sed splits on newlines, so this has to be handled before it gets there."""
    path = "/tmp/we\nird"
    assert bash_slug(path) == js_slug(path)
    assert py_slug(path) == js_slug(path)
