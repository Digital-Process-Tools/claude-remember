"""Regression pin for #405: EVERY script under scripts/ and every printed
message under pipeline/ must be pure ASCII, never just session-start-hook.sh
(#367).

#367 pinned the file-wide decision -- printed lines must never carry a byte
outside ASCII -- for session-start-hook.sh alone. #405 is the same decision
made once for the whole scripts/ tree instead of per file, because #367's own
reasoning (mojibake on a cp1252 Windows console) does not stop at that file's
boundary: doctor.sh and post-tool-hook.sh grew non-ASCII printed lines in the
very same delta that pinned session-start-hook.sh.

Scope, same as #367: PRINTED lines only -- bytes that reach stdout or stderr.
Comments are untouched; the em-dashes and arrows in this repo's prose are not
the subject of this rule.

Boundary decision (#405's own): pipeline/*.py is in scope too, and for a
sharper reason than cosmetics. pipeline/log.py's log() and pipeline/haiku.py's
_warn() fall back to print(..., file=sys.stderr) exactly like scripts/log.sh
falls back to echo ... >&2 -- but Python's print() DOES perform an encode
step against the console's codepage (unlike bash's echo), so a non-ASCII
printed message there is not cosmetic mojibake, it is the actual
UnicodeEncodeError crash #367's own issue said could not happen in bash.

One documented exception: scripts/bench-slug.sh's benchmark deliberately
prints a non-ASCII test path (cafe/nihongo/emoji) as its own subject matter --
it is a developer-only benchmark demonstrating non-ASCII path handling, not a
diagnostic read by an end user, and stripping the bytes would remove the
thing being measured. Every other non-ASCII byte in that same file (the
decorative "-- see lib-slug.sh" line) is fixed like everywhere else; only the
one benchmark-input line is carved out, by name, below.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
PIPELINE_DIR = REPO_ROOT / "pipeline"

# NOT anchored to the start of the line (a bare `.match`) on purpose: this
# codebase's own hooks routinely print via `[ cond ] && log ...`, `cmd || log
# ...` and `if cond; then log ...`, all of which put something other than the
# keyword first. `.search` with a word boundary catches every one of those.
_PRINT_KEYWORD = re.compile(r'\b(echo|printf|log|report_error)\b')
_COMMENT_LINE = re.compile(r'^\s*#')

# The one line in the whole scripts/ tree that is exempt: bench-slug.sh's own
# non-ASCII benchmark input, not a message to a human. Named exactly so a
# future non-ASCII line in this file cannot hide behind the same exemption
# by accident.
_BENCH_SLUG_EXEMPT_NEEDLE = "caf\u00e9"


def _shell_files() -> list[Path]:
    return sorted(SCRIPTS_DIR.glob("*.sh"))


def _non_comment_lines(path: Path) -> list[tuple[int, str]]:
    """Every line in `path` that is not a `#` comment (and, for
    bench-slug.sh, not its own named benchmark-input exemption).

    Deliberately not narrowed to lines that open with a print keyword: this
    codebase also builds a message in a `local _msg="..."` (or similarly
    named) assignment on one line and prints it via `log "$_msg"` several
    lines later (scripts/log.sh's `_dispatch_report_skip` is one such shape),
    so the message text a human eventually reads does not always share a
    line with the call that emits it. A keyword-anchored scan of THIS FILE
    was tried first and demonstrably missed both that shape and the
    `cond && log ...` guarded-print idiom -- reverting either shape back to
    an em-dash and rerunning the narrower scan still passed. Scanning every
    non-comment line closes both gaps at once, and is safe here because
    nothing in scripts/*.sh outside a comment or the named exemption is
    non-ASCII for a reason OTHER than eventually reaching a stream; if that
    ever changes (a byte pattern, a regex literal), name the new exemption
    the same way bench-slug.sh's is named below rather than loosening this
    scan back to something a guarded or assignment-carried message can hide
    behind again.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(lines, start=1):
        if _COMMENT_LINE.match(line):
            continue
        if path.name == "bench-slug.sh" and _BENCH_SLUG_EXEMPT_NEEDLE in line:
            continue
        out.append((i, line))
    return out


def _printed_shell_lines(path: Path) -> list[tuple[int, str]]:
    """The subset of `_non_comment_lines` that visibly names a print
    keyword. Used only for the per-file "known to have printed lines" floor
    below -- the non-ASCII assertion itself scans every non-comment line,
    for the reason given in `_non_comment_lines`."""
    return [
        (i, line) for i, line in _non_comment_lines(path)
        if _PRINT_KEYWORD.search(line)
    ]


def test_scanned_every_shell_file_under_scripts():
    """Positive control for the file enumeration itself: a scan that silently
    found zero files (wrong cwd, a glob typo) must not let every assertion
    below pass by vacuous truth."""
    files = _shell_files()
    assert len(files) >= 15, (
        f"expected at least 15 *.sh files under {SCRIPTS_DIR}, found "
        f"{len(files)} -- the scan likely did not read the real tree"
    )


def test_scan_finds_printed_lines_in_every_file_known_to_have_them():
    """Positive control for the per-file scan: files known to carry many
    echo/printf/log lines must still show up with a nonzero count, so a
    broken read of one specific file cannot pass by silently contributing
    zero lines to the aggregate below."""
    expect_nonzero = {
        "doctor.sh", "post-tool-hook.sh", "session-start-hook.sh",
        "log.sh", "save-session.sh", "user-prompt-hook.sh",
    }
    seen = {f.name: len(_printed_shell_lines(f)) for f in _shell_files()}
    missing = {name for name in expect_nonzero if seen.get(name, 0) == 0}
    assert not missing, (
        f"expected printed (echo/printf/log/report_error) lines in "
        f"{missing}, found none -- the scan likely did not read these files"
    )


def test_no_printed_shell_line_contains_non_ascii():
    """MUST FIRE (before the fix): doctor.sh, post-tool-hook.sh and several
    other scripts/*.sh files carried echo/printf/log lines with em-dashes
    (U+2014) or arrows (U+2192). After the fix, no printed line in any
    scripts/*.sh file may contain a byte outside ASCII, except the one named
    exemption in bench-slug.sh."""
    offenders = []
    for f in _shell_files():
        for n, l in _non_comment_lines(f):
            if any(ord(ch) > 127 for ch in l):
                offenders.append((f.name, n, l))
    assert not offenders, (
        "a non-comment line under scripts/ contains non-ASCII characters -- "
        "every script's printed lines must be pure ASCII (#405), and this "
        "scan deliberately covers every non-comment line rather than only "
        "ones that visibly name echo/printf/log/report_error, because a "
        "message this codebase builds in one assignment and prints several "
        "lines later would otherwise hide from a keyword-anchored scan:\n"
        + "\n".join(f"  {name}:{n}: {l!r}" for name, n, l in offenders)
    )


def test_comment_em_dashes_under_scripts_are_untouched():
    """Same boundary pin as #367's own test: the rule is scoped to PRINTED
    lines, so comment prose must survive completely untouched. A fix that
    stripped every em-dash tree-wide rather than deciding per-stream would
    pass the test above too, so this pins the boundary at the tree level."""
    comment_dashes = 0
    for f in _shell_files():
        text = f.read_text(encoding="utf-8")
        comment_dashes += sum(
            1 for line in text.splitlines()
            if _COMMENT_LINE.match(line) and "\u2014" in line
        )
    assert comment_dashes > 500, (
        "expected the scripts/ tree's comments to still carry plenty of "
        f"em-dashes (found {comment_dashes}) -- the fix must be scoped to "
        "printed lines, not a blanket strip across the tree"
    )


def test_bench_slug_benchmark_input_is_the_only_named_exemption():
    """The exemption above must name a real line, not silently match nothing
    (which would make the exemption a no-op and this whole test suite blind
    to a change that removed the benchmark input)."""
    text = (SCRIPTS_DIR / "bench-slug.sh").read_text(encoding="utf-8")
    assert _BENCH_SLUG_EXEMPT_NEEDLE in text, (
        "bench-slug.sh no longer contains its own non-ASCII benchmark input -- "
        "either the benchmark changed and this exemption is stale, or the "
        "benchmark was accidentally stripped"
    )


# ---------------------------------------------------------------------------
# pipeline/*.py: the same decision, argued for separately above. A regex scan
# over Python source cannot reliably tell a docstring from a printed f-string
# spanning several lines, so this walks the AST instead and collects every
# string literal (including f-string text segments) that is an argument to
# _warn(), log(), print(), or a raised exception's constructor.
# ---------------------------------------------------------------------------

_PRINTING_CALL_NAMES = {"_warn", "log", "print"}


def _string_constants(node: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            for v in n.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out.append(v.value)
    return out


def _printed_python_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    strings: list[str] = []
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name in _PRINTING_CALL_NAMES:
                target = node
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            target = node.exc
        if target is not None:
            strings.extend(_string_constants(target))
    return strings


def test_scanned_every_python_file_under_pipeline():
    """Positive control for the pipeline enumeration: a scan that silently
    found zero files must not let the assertion below pass by vacuous
    truth."""
    files = sorted(PIPELINE_DIR.glob("*.py"))
    assert len(files) >= 10, (
        f"expected at least 10 *.py files under {PIPELINE_DIR}, found "
        f"{len(files)} -- the scan likely did not read the real tree"
    )


def test_scan_finds_printed_calls_in_files_known_to_have_them():
    """Positive control for the AST walk itself: files known to call
    _warn()/log()/print() or raise with a message must contribute at least
    one string, so a broken parse of one specific file cannot pass by
    silently contributing nothing."""
    expect_nonzero = {"haiku.py", "log.py", "spawn_guard.py"}
    seen = {
        f.name: len(_printed_python_strings(f))
        for f in sorted(PIPELINE_DIR.glob("*.py"))
    }
    missing = {name for name in expect_nonzero if seen.get(name, 0) == 0}
    assert not missing, (
        f"expected _warn()/log()/print()/raise message strings in {missing}, "
        "found none -- the scan likely did not read these files"
    )


def test_no_printed_pipeline_string_contains_non_ascii():
    """MUST FIRE (before the fix): pipeline/haiku.py's _warn() calls and
    pipeline/spawn_guard.py's raised SummarizerSpawnDeclined message carried
    em-dashes, and pipeline/types.py's TokenUsage.__str__() (consumed by
    pipeline/log.py's log_tokens()) carried an arrow. Each of those reaches
    Python's print(..., file=sys.stderr) fallback, which -- unlike bash's
    echo -- performs a real encode step against the console's codepage, so
    this is the one place in this pin where the failure is not cosmetic
    mojibake but an actual UnicodeEncodeError crash."""
    offenders = []
    for f in sorted(PIPELINE_DIR.glob("*.py")):
        for s in _printed_python_strings(f):
            if any(ord(ch) > 127 for ch in s):
                offenders.append((f.name, s))
    assert not offenders, (
        "a string literal reaching _warn()/log()/print()/raise under "
        "pipeline/ contains non-ASCII characters -- every printed pipeline "
        "message must be pure ASCII (#405):\n"
        + "\n".join(f"  {name}: {s!r}" for name, s in offenders)
    )


def test_pipeline_docstrings_and_comments_carry_the_rest():
    """Same boundary pin as the shell-side test above, for the Python half:
    the rule is scoped to strings that are actually printed, so a module or
    function docstring or comment describing non-ASCII output (e.g.
    pipeline/types.py's own historical example) is untouched. A fix that
    stripped every non-ASCII byte from pipeline/*.py rather than deciding
    per-call would pass the test above too, so this pins the boundary."""
    printed = set()
    for f in sorted(PIPELINE_DIR.glob("*.py")):
        printed.update(_printed_python_strings(f))

    non_printed_non_ascii = 0
    for f in sorted(PIPELINE_DIR.glob("*.py")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if any(ord(ch) > 127 for ch in line) and line.strip() not in printed:
                non_printed_non_ascii += 1

    assert non_printed_non_ascii > 50, (
        "expected pipeline/ to still carry plenty of non-ASCII bytes outside "
        f"printed calls (found {non_printed_non_ascii}) -- the fix must be "
        "scoped to printed calls, not a blanket strip across the tree"
    )
