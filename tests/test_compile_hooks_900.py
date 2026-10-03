"""Hook self-containment, compiled at build time (#900).

The Anthropic plugin directory's release-preview validator inspects only
the command a `hooks/hooks.json` entry names; it never follows a
`source`/`.` statement into a second file, so each of the four
hooks.json-registered scripts was held as COMMAND_SCRIPT_NOT_FOLLOWED
(jit-context's own write-up, #900) for sourcing shared library code.
build_release_tree.py now inlines each one's own source chain at build
time (.github/scripts/compile_hooks.py) before it ever reaches
check_release_tree.py's FAIL guard (_check_hook_still_sources).

Every negative here is paired with a positive: a comment stripper that
drops nothing would still pass every "must preserve" assertion below, so
each "must be removed" case sits next to a "must survive" one, and the
synthetic end-to-end test below pins the one claim a unit test cannot:
that sourcing N files and inlining the same N files, include-guard
deduped, produce byte-identical *behaviour* when actually run.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "compile_hooks.py"
SCRIPTS_DIR = REPO_ROOT / "scripts"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#900)"
    spec = importlib.util.spec_from_file_location("compile_hooks", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["compile_hooks"] = mod
    spec.loader.exec_module(mod)
    return mod


compile_hooks = _load()


def _bash() -> str:
    b = __import__("shutil").which("bash")
    if not b:
        pytest.skip("no bash on PATH")
    return b


# -- strip_whole_line_comments -----------------------------------------------

def test_strip_drops_a_whole_line_comment():
    text = 'echo hi\n# this whole line is a comment\necho bye\n'
    out = compile_hooks.strip_whole_line_comments(text)
    assert "# this whole line is a comment" not in out
    assert "echo hi" in out and "echo bye" in out


def test_strip_drops_an_indented_comment_line():
    text = 'if true; then\n    # indented comment\n    echo hi\nfi\n'
    out = compile_hooks.strip_whole_line_comments(text)
    assert "# indented comment" not in out
    assert "echo hi" in out


def test_strip_keeps_the_files_own_shebang():
    text = '#!/usr/bin/env bash\n# a real comment\necho hi\n'
    out = compile_hooks.strip_whole_line_comments(text)
    assert out.splitlines()[0] == "#!/usr/bin/env bash"
    assert "# a real comment" not in out


def test_strip_never_touches_an_inline_trailing_comment():
    # Scope is whole-LINE comments only -- a line that is code with a
    # trailing comment is left completely alone, text and all.
    text = 'echo hi  # trailing, not stripped\n'
    out = compile_hooks.strip_whole_line_comments(text)
    assert out == text


def test_strip_never_touches_a_heredoc_body():
    text = (
        'cat <<EOF\n'
        '# this looks like a comment but is heredoc content\n'
        'EOF\n'
        '# this one really is a comment\n'
    )
    out = compile_hooks.strip_whole_line_comments(text)
    assert "# this looks like a comment but is heredoc content" in out
    assert "# this one really is a comment" not in out


def test_strip_never_touches_a_line_inside_an_open_double_quote():
    # A multi-line double-quoted string whose SECOND line merely looks
    # like a whole-line comment must survive untouched -- the opening
    # quote on line 1 is still open when line 2 starts.
    text = 'VAR="line one\n# not a comment -- inside the string\nline two"\n'
    out = compile_hooks.strip_whole_line_comments(text)
    assert out == text


def test_strip_never_touches_a_line_inside_an_open_single_quote():
    text = "VAR='line one\n# not a comment either\nline two'\n"
    out = compile_hooks.strip_whole_line_comments(text)
    assert out == text


def test_strip_is_idempotent():
    text = 'echo hi\n# comment\necho bye\n'
    once = compile_hooks.strip_whole_line_comments(text)
    twice = compile_hooks.strip_whole_line_comments(once)
    assert once == twice


# -- inline_sources -----------------------------------------------------

def test_inline_sources_substitutes_a_sibling_file():
    contents = {
        "scripts/hook.sh": '#!/usr/bin/env bash\nsource "${X}/scripts/lib.sh"\necho after\n',
        "scripts/lib.sh": '#!/usr/bin/env bash\necho from-lib\n',
    }
    out = compile_hooks.inline_sources("scripts/hook.sh", contents)
    assert 'source "${X}/scripts/lib.sh"' not in out
    assert "echo from-lib" in out
    assert "echo after" in out


def test_inline_sources_include_guard_dedups_a_diamond():
    # hook sources A and B; B also sources A -- A's body must appear
    # exactly once in the final output (include-guard semantics).
    contents = {
        "scripts/hook.sh": (
            'source "${X}/scripts/a.sh"\n'
            'source "${X}/scripts/b.sh"\n'
        ),
        "scripts/a.sh": 'echo from-a\n',
        "scripts/b.sh": 'source "${X}/scripts/a.sh"\necho from-b\n',
    }
    out = compile_hooks.inline_sources("scripts/hook.sh", contents)
    assert out.count("echo from-a") == 1
    assert "echo from-b" in out
    assert 'source "${X}/scripts/a.sh"' not in out


def test_inline_sources_raises_on_cycle():
    contents = {
        "scripts/hook.sh": 'source "${X}/scripts/a.sh"\n',
        "scripts/a.sh": 'source "${X}/scripts/hook.sh"\n',
    }
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.inline_sources("scripts/hook.sh", contents)


def test_inline_sources_raises_on_missing_target():
    contents = {"scripts/hook.sh": 'source "${X}/scripts/missing.sh"\n'}
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.inline_sources("scripts/hook.sh", contents)


def test_inline_sources_raises_on_unresolvable_target():
    contents = {"scripts/hook.sh": 'source "${X}/not-a-shell-file"\n'}
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.inline_sources("scripts/hook.sh", contents)


def test_source_line_does_not_match_a_quoted_argument_named_source():
    # A function-call argument literally spelled "source" must not be
    # mistaken for a sourcing statement (the exact false-positive this
    # repo's own session-start-hook.sh:334 would otherwise trip on:
    # `_stdin_json_string_into VAR source "$HOOK_STDIN"`).
    line = '_stdin_json_string_into SESSION_START_SOURCE source "$HOOK_STDIN"'
    assert compile_hooks.unresolved_sources(line) == []


def test_unresolved_sources_reports_remaining_lines():
    text = 'echo hi\nsource "${X}/scripts/lib.sh"\n'
    remaining = compile_hooks.unresolved_sources(text)
    assert len(remaining) == 1
    assert remaining[0][0] == 2


# -- the two real shapes self-review found the first draft missing --------
#
# A naive regex anchored at line-start (`^source ...`) misses both of these
# real patterns from this repo's own hooks -- confirmed by direct
# inspection during review (session-start-hook.sh:217,
# post-tool-hook.sh:833) -- so both are pinned here against the real text,
# not just a synthetic stand-in.

def test_inline_sources_handles_an_assignment_prefixed_trailing_guard():
    # REMEMBER_PATHS_SOFT_FAIL=1 source "..." || exit 0 -- a VAR=value
    # prefix before a shell BUILTIN (source/. is one) persists in the
    # current shell afterward, so it must be emitted as its own statement;
    # the trailing `|| exit 0` guards against sourcing FAILING, which
    # inlining makes moot, so it must be dropped rather than left dangling.
    contents = {
        "scripts/hook.sh": (
            'FOO=1 source "${X}/scripts/lib.sh" || exit 0\n'
            'echo after\n'
        ),
        "scripts/lib.sh": 'echo from-lib\n',
    }
    out = compile_hooks.inline_sources("scripts/hook.sh", contents)
    assert "FOO=1" in out
    assert "echo from-lib" in out
    assert "echo after" in out
    assert "|| exit 0" not in out
    assert compile_hooks.unresolved_sources(out) == []


def test_inline_sources_preserves_a_conditional_gate():
    # COND || source "..." decides WHETHER sourcing happens at all (a
    # lazy-init / cost-avoidance gate) -- dropping COND would be a
    # behavior change, not a correctness-neutral inlining. The condition
    # must survive, with the inlined body as the right-hand side of the
    # same `||`.
    contents = {
        "scripts/hook.sh": (
            '[ -n "${ALREADY:-}" ] || source "${X}/scripts/lib.sh"\n'
            'echo after\n'
        ),
        "scripts/lib.sh": 'echo from-lib\n',
    }
    out = compile_hooks.inline_sources("scripts/hook.sh", contents)
    assert '[ -n "${ALREADY:-}" ] ||' in out
    assert "echo from-lib" in out
    assert "echo after" in out
    assert compile_hooks.unresolved_sources(out) == []


def test_source_detection_never_fires_inside_an_unrelated_single_quoted_string():
    # The exact false-positive self-review found: a `;`/`.`-shaped jq
    # filter sitting inside a SINGLE-QUOTED bash argument, on the same
    # physical line as a real statement earlier in the file -- confirmed
    # against this repo's own scripts/lib-memory-dir.sh merge-config jq
    # script, which contains the literal text "; . * $x" as jq syntax.
    line = ("jq -s 'reduce .[] as $x ({}; . * $x) | with_entries(select"
            '(.key | startswith("_") | not))\' "${_jq_merge_sources[@]}" '
            '> "$_merged_cfg" 2>/dev/null')
    assert compile_hooks.unresolved_sources(line) == []


def test_inline_sources_does_not_match_inside_an_unrelated_quoted_string():
    # Positive control for the test above: a REAL source statement must
    # still be found and inlined even in a file that elsewhere carries an
    # unrelated quoted jq-like filter -- the quote-awareness must never
    # swallow a genuine statement, only skip unrelated quoted text.
    contents = {
        "scripts/hook.sh": (
            'source "${X}/scripts/lib.sh"\n'
            "jq -s 'reduce .[] as $x ({}; . * $x)' > /dev/null\n"
        ),
        "scripts/lib.sh": 'echo from-lib\n',
    }
    out = compile_hooks.inline_sources("scripts/hook.sh", contents)
    assert "echo from-lib" in out
    assert "jq -s" in out
    assert compile_hooks.unresolved_sources(out) == []


def test_a_real_statement_followed_by_unrelated_code_on_the_same_line_fails_loudly():
    # A `;`-separated REAL second statement after a plain source call is
    # not a redirect/exit-status guard and is not observed anywhere in
    # this repo's own hooks -- InlineError is the correct, safe response
    # (never silently drop code that was not a recognised guard shape).
    contents = {
        "scripts/hook.sh": 'source "${X}/scripts/lib.sh"; echo also-this\n',
        "scripts/lib.sh": 'echo from-lib\n',
    }
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.inline_sources("scripts/hook.sh", contents)


# -- synthetic end-to-end: sourced vs compiled must behave identically ------

_LIB_CLOCK = (
    '#!/usr/bin/env bash\n'
    '[ -n "${_T_LIB_CLOCK_LOADED:-}" ] && return 0\n'
    '_T_LIB_CLOCK_LOADED=1\n'
    'now_epoch() { echo 1700000000; }\n'
)
_LIB_SLUG = (
    '#!/usr/bin/env bash\n'
    '[ -n "${_T_LIB_SLUG_LOADED:-}" ] && return 0\n'
    '_T_LIB_SLUG_LOADED=1\n'
    'slugify() { echo "slug-$1"; }\n'
)
# Mirrors this repo's own shape: bootstrap sources slug a SECOND time --
# lib-slug's own runtime guard makes that already a no-op today, and the
# compiled form drops the repeat source line entirely (include-guard).
_LIB_BOOTSTRAP = (
    '#!/usr/bin/env bash\n'
    'source "${T_ROOT}/scripts/lib-slug.sh"\n'
    'bootstrap_marker() { echo bootstrapped; }\n'
)
_HOOK = (
    '#!/usr/bin/env bash\n'
    'set -u\n'
    'source "${T_ROOT}/scripts/lib-clock.sh"\n'
    'source "${T_ROOT}/scripts/lib-slug.sh"\n'
    'source "${T_ROOT}/scripts/lib-bootstrap.sh"\n'
    '# a whole-line comment the compiled build must drop\n'
    'echo "time=$(now_epoch)"\n'
    'echo "$(slugify my-project)"\n'
    'echo "$(bootstrap_marker)"\n'
)


def _write_fixture(root: Path) -> dict:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    files = {
        "lib-clock.sh": _LIB_CLOCK,
        "lib-slug.sh": _LIB_SLUG,
        "lib-bootstrap.sh": _LIB_BOOTSTRAP,
        "hook.sh": _HOOK,
    }
    for name, text in files.items():
        (scripts / name).write_text(text, encoding="utf-8")
    return {f"scripts/{name}": text for name, text in files.items()}


def test_compiled_hook_behaves_identically_to_the_sourced_one(tmp_path):
    contents = _write_fixture(tmp_path)
    compiled_text = compile_hooks.compile_hook("scripts/hook.sh", contents)
    assert compile_hooks.unresolved_sources(compiled_text) == []
    compiled_path = tmp_path / "scripts" / "hook-compiled.sh"
    compiled_path.write_text(compiled_text, encoding="utf-8")

    env = {**os.environ, "T_ROOT": str(tmp_path)}
    original = subprocess.run(
        [_bash(), str(tmp_path / "scripts" / "hook.sh")],
        env=env, capture_output=True, text=True, check=False,
    )
    compiled = subprocess.run(
        [_bash(), str(compiled_path)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert original.returncode == compiled.returncode == 0
    assert original.stdout == compiled.stdout
    assert original.stdout.splitlines() == [
        "time=1700000000", "slug-my-project", "bootstrapped",
    ]


# -- real-repo regression: the actual four hooks compile cleanly -----------

def _sh_texts() -> dict:
    return {f"scripts/{p.name}": p.read_text(encoding="utf-8")
            for p in SCRIPTS_DIR.glob("*.sh")}


@pytest.mark.parametrize("hook_name", compile_hooks.HOOK_SCRIPT_NAMES)
def test_real_hook_compiles_self_contained_and_under_budget(hook_name):
    key = f"scripts/{hook_name}"
    contents = _sh_texts()
    if key not in contents:
        pytest.skip(f"{key} not present in this checkout")
    compiled = compile_hooks.compile_hook(key, contents)
    assert compile_hooks.unresolved_sources(compiled) == [], (
        f"{hook_name}: still sources another file after compiling"
    )
    size = len(compiled.encode("utf-8"))
    # #900's own stop-threshold: fail loudly here rather than ship a
    # compiled hook that a future library addition could push over the
    # directory's real 256 KiB budget with no headroom left to notice.
    assert size < 230 * 1024, f"{hook_name}: compiled to {size} bytes, over the 230 KiB stop-threshold"


@pytest.mark.parametrize("hook_name", compile_hooks.HOOK_SCRIPT_NAMES)
def test_real_hook_compiles_to_syntactically_valid_bash(hook_name):
    key = f"scripts/{hook_name}"
    contents = _sh_texts()
    if key not in contents:
        pytest.skip(f"{key} not present in this checkout")
    compiled = compile_hooks.compile_hook(key, contents)
    result = subprocess.run(
        [_bash(), "-n"], input=compiled, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"{hook_name}: compiled output fails `bash -n`:\n{result.stderr}"
