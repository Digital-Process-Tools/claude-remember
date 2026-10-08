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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ._compiled_hooks import is_compiled_text

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


# -- link_sources -------------------------------------------------------

_CALL = '${1+"$@"}'


def test_link_sources_turns_a_source_into_a_call_and_keeps_the_file():
    contents = {
        "scripts/hook.sh": '#!/usr/bin/env bash\nsource "${X}/scripts/lib.sh"\necho after\n',
        "scripts/lib.sh": '#!/usr/bin/env bash\necho from-lib\n',
    }
    body, libs = compile_hooks.link_sources("scripts/hook.sh", contents)
    assert 'source "${X}/scripts/lib.sh"' not in body
    assert f"__remember_src_lib {_CALL}" in body.splitlines()
    assert "echo after" in body
    assert list(libs) == ["scripts/lib.sh"]
    assert "echo from-lib" in libs["scripts/lib.sh"]


def test_link_sources_defines_one_wrapper_per_file_in_a_diamond():
    # hook sources A and B; B also sources A -- A is ONE library (one
    # wrapper definition), called from both sites. Whether the second call
    # does anything is A's own runtime guard's business, exactly as with
    # `source` (see the round-4 behaviour tests below).
    contents = {
        "scripts/hook.sh": (
            'source "${X}/scripts/a.sh"\n'
            'source "${X}/scripts/b.sh"\n'
        ),
        "scripts/a.sh": 'echo from-a\n',
        "scripts/b.sh": 'source "${X}/scripts/a.sh"\necho from-b\n',
    }
    body, libs = compile_hooks.link_sources("scripts/hook.sh", contents)
    assert list(libs) == ["scripts/a.sh", "scripts/b.sh"]
    assert f"__remember_src_a {_CALL}" in libs["scripts/b.sh"]
    assert body.count("__remember_src_a ") == 1
    compiled = compile_hooks.compile_hook("scripts/hook.sh", contents)
    assert compiled.count("echo from-a") == 1
    assert 'source "${X}/scripts/a.sh"' not in compiled


def test_link_sources_raises_on_cycle():
    contents = {
        "scripts/hook.sh": 'source "${X}/scripts/a.sh"\n',
        "scripts/a.sh": 'source "${X}/scripts/hook.sh"\n',
    }
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.link_sources("scripts/hook.sh", contents)


def test_link_sources_raises_on_missing_target():
    contents = {"scripts/hook.sh": 'source "${X}/scripts/missing.sh"\n'}
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.link_sources("scripts/hook.sh", contents)


def test_link_sources_raises_on_unresolvable_target():
    contents = {"scripts/hook.sh": 'source "${X}/not-a-shell-file"\n'}
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.link_sources("scripts/hook.sh", contents)


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
# not just a synthetic stand-in. Round 4: neither the prefix nor the
# trailing guard is rewritten any more -- both apply to the call exactly as
# they applied to `source` (the behaviour tests below run both forms).

def test_link_sources_keeps_an_assignment_prefix_and_trailing_guard_on_the_call():
    # REMEMBER_PATHS_SOFT_FAIL=1 source "..." || exit 0: the `|| exit 0` is
    # NOT dead code -- resolve-paths.sh `return 1`s on a soft failure and
    # this guard is what stops the hook. It stays, on the call.
    contents = {
        "scripts/hook.sh": (
            'FOO=1 source "${X}/scripts/lib.sh" || exit 0\n'
            'echo after\n'
        ),
        "scripts/lib.sh": 'echo from-lib\n',
    }
    body, libs = compile_hooks.link_sources("scripts/hook.sh", contents)
    assert f"FOO=1 __remember_src_lib {_CALL} || exit 0" in body.splitlines()
    assert "echo after" in body
    assert "echo from-lib" in libs["scripts/lib.sh"]
    assert compile_hooks.unresolved_sources(body) == []


def test_link_sources_preserves_a_conditional_gate():
    # COND || source "..." decides WHETHER sourcing happens at all (a
    # lazy-init / cost-avoidance gate) -- dropping COND would be a
    # behavior change. The condition survives, gating the call.
    contents = {
        "scripts/hook.sh": (
            '[ -n "${ALREADY:-}" ] || source "${X}/scripts/lib.sh"\n'
            'echo after\n'
        ),
        "scripts/lib.sh": 'echo from-lib\n',
    }
    body, _libs = compile_hooks.link_sources("scripts/hook.sh", contents)
    assert f'[ -n "${{ALREADY:-}}" ] || __remember_src_lib {_CALL}' in body.splitlines()
    assert compile_hooks.unresolved_sources(body) == []


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


def test_link_sources_does_not_match_inside_an_unrelated_quoted_string():
    # Positive control for the test above: a REAL source statement must
    # still be found and linked even in a file that elsewhere carries an
    # unrelated quoted jq-like filter -- the quote-awareness must never
    # swallow a genuine statement, only skip unrelated quoted text.
    contents = {
        "scripts/hook.sh": (
            'source "${X}/scripts/lib.sh"\n'
            "jq -s 'reduce .[] as $x ({}; . * $x)' > /dev/null\n"
        ),
        "scripts/lib.sh": 'echo from-lib\n',
    }
    body, libs = compile_hooks.link_sources("scripts/hook.sh", contents)
    assert list(libs) == ["scripts/lib.sh"]
    assert "jq -s 'reduce .[] as $x ({}; . * $x)' > /dev/null" in body
    assert compile_hooks.unresolved_sources(body) == []


def test_link_sources_raises_on_a_single_quoted_sh_target():
    # Audit finding (Class B): a single-quoted target naming a real `.sh`
    # sibling must fail loudly, not be silently passed through unresolved
    # the way the first SOURCE_LINE-only draft did.
    contents = {"scripts/hook.sh": "source '${X}/scripts/lib.sh'\n"}
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.link_sources("scripts/hook.sh", contents)


def test_link_sources_raises_on_a_bare_unquoted_sh_target():
    # Same finding, the other spelling: no quotes at all.
    contents = {"scripts/hook.sh": "source ${X}/scripts/lib.sh\n"}
    with pytest.raises(compile_hooks.InlineError):
        compile_hooks.link_sources("scripts/hook.sh", contents)


def test_cli_exits_non_zero_when_a_hook_name_is_missing(tmp_path):
    # Audit finding (Class A): main()'s "not found" branch used to leave
    # `status` at 0, so a caller reading only the exit code (the new CI
    # job's own compile step does exactly this) would see success even
    # though nothing compiled.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "lib.sh").write_text("echo hi\n", encoding="utf-8")
    rc = compile_hooks.main(["--repo", str(tmp_path)])
    assert rc != 0


def test_a_real_statement_after_a_source_on_the_same_line_is_kept_intact():
    # Rounds 1-3 refused this (InlineError): pasting a file's body in place
    # of `source` had nowhere to put the rest of the line. A call does --
    # the rest of the line is simply left where it was.
    contents = {
        "scripts/hook.sh": 'source "${X}/scripts/lib.sh"; echo also-this\n',
        "scripts/lib.sh": 'echo from-lib\n',
    }
    body, _libs = compile_hooks.link_sources("scripts/hook.sh", contents)
    assert f"__remember_src_lib {_CALL}; echo also-this" in body.splitlines()


@pytest.mark.parametrize("stmt", [
    "local x=1",
    "declare -a arr=()",
    "typeset y",
    "shift",
    "set -- a b",
    "if true; then local z; fi",
])
def test_a_library_statement_that_changes_meaning_in_a_function_fails_the_build(stmt):
    # A sourced file's top-level `local`/`declare` and `shift`/`set --`
    # would mean something else inside the wrapper function -- refused
    # loudly rather than shipped with a changed meaning.
    contents = {
        "scripts/hook.sh": 'source "${X}/scripts/lib.sh"\n',
        "scripts/lib.sh": f'{stmt}\necho from-lib\n',
    }
    with pytest.raises(compile_hooks.InlineError, match="would mean something different"):
        compile_hooks.compile_hook("scripts/hook.sh", contents)


def test_a_librarys_own_functions_may_use_local_and_queries_are_fine():
    # Positive control for the refusal above: `local` inside a function
    # the library defines -- at column 0 or indented inside an `if`, the
    # shape detect-tools.sh's lazy `_remember_python` has -- is that
    # function's own, and a `declare -F`/`-f` query changes nothing.
    contents = {
        "scripts/hook.sh": 'source "${X}/scripts/lib.sh"\nf\ng\n',
        "scripts/lib.sh": (
            'declare -F log >/dev/null 2>&1 || log() { :; }\n'
            'f() {\n'
            '    local a=1\n'
            '}\n'
            'if true; then\n'
            '    g() {\n'
            '        local b=2\n'
            '    }\n'
            'fi\n'
        ),
    }
    compiled = compile_hooks.compile_hook("scripts/hook.sh", contents)
    assert "local a=1" in compiled and "local b=2" in compiled


def test_tree_shake_drops_an_unreached_function_defined_inside_a_wrapper():
    # A library's functions sit one level inside its wrapper; they are
    # shaken like top-level ones (positive AND negative control), and the
    # wrapper itself -- called from root code -- always stays.
    contents = {
        "scripts/hook.sh": '#!/usr/bin/env bash\nsource "${X}/scripts/lib.sh"\nused_fn\n',
        "scripts/lib.sh": (
            'used_fn() {\n'
            '    echo used\n'
            '}\n'
            'unused_fn() {\n'
            '    echo unused\n'
            '}\n'
        ),
    }
    text, report = compile_hooks.compile_hook_report("scripts/hook.sh", contents)
    assert report["shaken"] is True, report["reason"]
    assert report["dropped"] == ["unused_fn"]
    assert "used_fn() {" in text and "echo unused" not in text
    assert "__remember_src_lib() {" in text
    r = subprocess.run([_bash(), "-c", text], capture_output=True, text=True, check=False,
                       env={**os.environ, "X": "/nonexistent"})
    assert r.stdout == "used\n", r.stderr


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


# -- tree-shaking (#900 round 2): drop a function the hook never reaches --
#
# review of the combined tree (fix/898 round 3 + this lane's round 1) found
# that inlining a library whole, with none of its functions dropped, ships
# code the hook's own control flow never reaches -- exactly the "perl code"
# shape jit-context's own compile_scripts.py was held for (#461) before it
# added tree-shaking. compile_hooks.tree_shake is modelled on that function:
# a function is dropped only when its own name never appears as a bare word
# anywhere outside a function definition, or inside an already-kept
# function's body, transitively.

def test_tree_shake_drops_an_unreached_function_and_keeps_a_reached_one():
    # Positive AND negative control in one fixture, per the issue's own
    # ask: 'used' is called from root code and must survive; 'unused' is
    # never called from anywhere and must be dropped.
    text = (
        "#!/bin/sh\n"
        "used() {\n"
        "    echo used\n"
        "}\n"
        "unused() {\n"
        "    echo unused\n"
        "}\n"
        "used\n"
    )
    out, report = compile_hooks.tree_shake(text)
    assert report["shaken"] is True
    assert report["kept"] == ["used"]
    assert report["dropped"] == ["unused"]
    assert "unused" not in out
    assert "used" in out


def test_tree_shake_keeps_a_function_reached_only_transitively():
    text = (
        "#!/bin/sh\n"
        "a() {\n"
        "    b\n"
        "}\n"
        "b() {\n"
        "    echo b\n"
        "}\n"
        "c() {\n"
        "    echo c\n"
        "}\n"
        "a\n"
    )
    out, report = compile_hooks.tree_shake(text)
    assert set(report["kept"]) == {"a", "b"}
    assert report["dropped"] == ["c"]
    assert "echo c" not in out
    assert "echo b" in out


def test_tree_shake_keeps_everything_when_dispatch_is_dynamic():
    # A call through a lowercase/mixed-case variable (`"$FN"`) names a
    # target this textual analysis cannot resolve -- nothing may be
    # dropped, and the report must say why rather than silently shaking
    # anyway.
    text = (
        '#!/bin/sh\n'
        'real_fn() {\n'
        "    echo real\n"
        "}\n"
        'fn="real_fn"\n'
        '"$fn"\n'
    )
    out, report = compile_hooks.tree_shake(text)
    assert report["shaken"] is False
    assert report["dynamic_dispatch"] is True
    assert report["dropped"] == []
    assert "real_fn" in out


def test_tree_shake_an_all_caps_variable_call_is_not_dynamic_dispatch():
    # $PYTHON/$JQ-shaped calls (this repo's own external-tool convention)
    # must not themselves trigger the conservative bail -- otherwise
    # nothing in any real hook here could ever be shaken (all four call
    # $PYTHON this way).
    text = (
        "#!/bin/sh\n"
        "unused() {\n"
        "    echo unused\n"
        "}\n"
        '"${PYTHON:-python3}" -c "pass"\n'
    )
    _out, report = compile_hooks.tree_shake(text)
    assert report["dynamic_dispatch"] is False
    assert report["shaken"] is True
    assert report["dropped"] == ["unused"]


def test_tree_shake_a_lowercase_test_operand_inside_double_brackets_is_not_dynamic_dispatch():
    # #906: `&& "$x"` is a real code-position match for _DYNAMIC_CALL_RE
    # when it sits inside `[[ ... ]]`, but there it is a TEST OPERAND, not
    # a call through a variable -- `[[ ]]` cannot invoke an arbitrary
    # command. Shaking must still proceed and still drop the unused
    # function; this is the exact shape from scripts/lib-lock.sh that grew
    # the compiled hook by 10+ KB with no visible symptom besides size.
    text = (
        "#!/bin/sh\n"
        "unused() {\n"
        "    echo unused\n"
        "}\n"
        'x="abc"\n'
        '[[ -n "$x" && "$x" != *[!0-9]* ]] && echo digits\n'
    )
    out, report = compile_hooks.tree_shake(text)
    assert report["dynamic_dispatch"] is False
    assert report["shaken"] is True
    assert report["dropped"] == ["unused"]
    assert "echo unused" not in out


def test_tree_shake_a_lowercase_test_operand_inside_single_brackets_is_not_dynamic_dispatch():
    # Same false-positive shape, classic `[ ... ]` test form.
    text = (
        "#!/bin/sh\n"
        "unused() {\n"
        "    echo unused\n"
        "}\n"
        'x="abc"\n'
        '[ -n "$x" ] && [ "$x" != "" ] && echo nonempty\n'
    )
    out, report = compile_hooks.tree_shake(text)
    assert report["dynamic_dispatch"] is False
    assert report["shaken"] is True
    assert report["dropped"] == ["unused"]
    assert "echo unused" not in out


def test_tree_shake_a_real_call_through_a_variable_still_bails_even_after_a_test():
    # The fix for the false positive above must not also blind the real
    # positive: a genuine dynamic dispatch (`"$fn"` in command position)
    # appearing on a line AFTER a `[[ ]]` test must still disable shaking.
    text = (
        "#!/bin/sh\n"
        "real_fn() {\n"
        "    echo real\n"
        "}\n"
        'fn="real_fn"\n'
        '[[ -n "$fn" ]] && "$fn"\n'
    )
    out, report = compile_hooks.tree_shake(text)
    assert report["shaken"] is False
    assert report["dynamic_dispatch"] is True
    assert report["dropped"] == []
    assert "real_fn" in out


def test_tree_shake_a_test_operand_split_across_lines_by_implicit_continuation_is_not_dynamic_dispatch():
    # Review finding on #906's own fix: bash continues a statement onto the
    # next physical line not only after an explicit trailing backslash, but
    # also whenever a line simply ends on '&&'/'||'/'|' -- no backslash at
    # all (this repo's own scripts/run-consolidation.sh:301 line-wraps
    # exactly this way). A `[[ ... ]]` test split across such a boundary
    # must still be recognized as a test span, not scanned per-line raw.
    text = (
        "#!/bin/sh\n"
        "unused() {\n"
        "    echo unused\n"
        "}\n"
        'x="abc"\n'
        '[[ -n "$x" &&\n'
        '   "$x" != "" ]] && echo nonempty\n'
    )
    out, report = compile_hooks.tree_shake(text)
    assert report["dynamic_dispatch"] is False
    assert report["shaken"] is True
    assert report["dropped"] == ["unused"]
    assert "echo unused" not in out


def test_tree_shake_reason_names_the_triggering_line_when_dispatch_is_dynamic():
    # The report's "reason" string must name the physical line that
    # triggered the bail -- a silent fallback is the failure mode #906
    # calls out; the only prior symptom was the compiled file's byte size.
    text = (
        '#!/bin/sh\n'
        'real_fn() {\n'
        "    echo real\n"
        "}\n"
        'fn="real_fn"\n'
        '"$fn"\n'
    )
    _out, report = compile_hooks.tree_shake(text)
    assert report["dynamic_dispatch"] is True
    assert "line 6" in report["reason"]


def test_tree_shake_correctly_finds_a_functions_end_despite_a_nested_brace_group():
    # The exact risk this module's own inliner introduces: a gated source
    # statement's `{ ...; }` wrapper can land INSIDE a function body (this
    # repo's own session-start-hook.sh lazily sources lib-lock.sh/
    # lib-case-divergence.sh from inside a function). A naive "the next
    # bare '}' line ends the function" scan would stop at the nested
    # group's own close; depth-tracking must not.
    text = (
        "#!/bin/sh\n"
        "outer() {\n"
        '    COND=1\n'
        '    if [ "$COND" = 1 ]; then\n'
        "        : || {\n"
        "            echo nested\n"
        "        }\n"
        "    fi\n"
        "}\n"
        "unused_fn() {\n"
        "    echo unused\n"
        "}\n"
        "outer\n"
    )
    out, report = compile_hooks.tree_shake(text)
    assert report["shaken"] is True
    assert report["dropped"] == ["unused_fn"]
    assert "echo nested" in out
    assert "fi" in out


def test_tree_shake_handles_the_param_expansion_hash_correctly():
    # `${raw#*"$key"}` has a literal '#' that is pattern-removal syntax,
    # not a comment -- treating it as one stops the brace-balance scan
    # before this line's own closing '}', which is exactly the false
    # "unbalanced braces" alarm this repo's own compiled
    # scripts/session-end-hook.sh produced during this module's own
    # development (line 79, `rest=${raw#*\"$key\"}`) before the fix.
    text = (
        "#!/bin/sh\n"
        "used() {\n"
        '    rest=${raw#*\\"$key\\"}\n'
        "    echo \"$rest\"\n"
        "}\n"
        "unused() {\n"
        "    echo unused\n"
        "}\n"
        "used\n"
    )
    _out, report = compile_hooks.tree_shake(text)
    assert report["shaken"] is True
    assert report["dropped"] == ["unused"]


def test_tree_shake_does_not_mistake_a_mid_line_string_continuation_for_code():
    # A multi-line double-quoted string whose continuation line happens to
    # start, after masking, with something `$`-shaped must never be read
    # as a command -- the exact false positive this module's own
    # development hit against scripts/user-prompt-hook.sh:386
    # (a bare `$_notice_body"` continuation line inside a help/log string).
    text = (
        "#!/bin/sh\n"
        "used() {\n"
        '    msg="line one\n'
        "  $_candidate: on PATH ($(command -v \"$_first\" 2>/dev/null))\n"
        '"\n'
        "    echo \"$msg\"\n"
        "}\n"
        "unused() {\n"
        "    echo unused\n"
        "}\n"
        "used\n"
    )
    _out, report = compile_hooks.tree_shake(text)
    assert report["dynamic_dispatch"] is False
    assert report["shaken"] is True
    assert report["dropped"] == ["unused"]


def test_tree_shake_refuses_to_guess_on_unbalanced_braces():
    text = "#!/bin/sh\nfoo() {\n    echo unbalanced\n"
    out, report = compile_hooks.tree_shake(text)
    assert report["shaken"] is False
    assert "unbalanced" in report["reason"]
    assert out == text


def test_tree_shake_handles_a_command_substitution_spanning_multiple_lines():
    # #900 round 3: a "$(... <<< '...')" here-string reading a multi-line
    # Python script, the shape fix/898's own heredoc-to-here-string
    # rewrite introduced -- a verbatim excerpt of the real
    # session-start-hook.sh's own `session_was_saved` (renamed `used`
    # here), which tripped this exact false "unbalanced braces" alarm:
    # _command_substitution_end only resolved a '$(...)' that closed on
    # the SAME physical line, so falling through to ordinary
    # per-character handling for this unresolved (multi-line) substitution
    # let its own internal quotes leak into and desync the outer quote
    # state, silently swallowing `used`'s own closing '}' several lines
    # later. The closing function here must still be found, and the
    # embedded Python's own "return (" + lowercase-identifier-shaped lines
    # (the text _cmdsub_continue must keep masked, not reveal) must not be
    # misread as bash dynamic dispatch either. Confirmed red against this
    # module's pre-fix form (reported "a quote or heredoc never closed by
    # end of file") before this fix made it green.
    text = (
        "#!/bin/sh\n"
        "used() {\n"
        "    [ -n \"$1\" ] && [ -f \"$LAST_SAVE_FILE\" ] || return 1\n"
        "    if [ \"$JQ\" = \"_jq_fallback\" ]; then\n"
        "        _remember_python || return 1\n"
        "        [ \"$(_remember_run_python - \"$LAST_SAVE_FILE\" \"$1\" <<< 'import json, math, sys\n"
        "\n"
        "def isline(v):\n"
        "    # Mirrors $SAVED_QUERY'\"'\"'s own `isline` def exactly: a JSON number,\n"
        "    return (\n"
        "        isinstance(v, (int, float))\n"
        "        and not isinstance(v, bool)\n"
        "        and math.isfinite(v)\n"
        "        and v == math.floor(v)\n"
        "    )\n"
        "\n"
        "try:\n"
        "    data = json.load(open(sys.argv[1]))\n"
        "except Exception:\n"
        "    print(\"unsaved\")\n"
        "    sys.exit(0)\n"
        "\n"
        "sid = sys.argv[2]\n"
        "if not isinstance(data, dict):\n"
        "    print(\"unsaved\")\n"
        "    sys.exit(0)\n"
        "\n"
        "sessions = data.get(\"sessions\")\n"
        "if sessions is not None and not isinstance(sessions, dict):\n"
        "    print(\"unsaved\")\n"
        "    sys.exit(0)\n"
        "\n"
        "if isinstance(sessions, dict) and isline(sessions.get(sid)):\n"
        "    print(\"saved\")\n"
        "elif data.get(\"session\") == sid and isline(data.get(\"line\")):\n"
        "    print(\"saved\")\n"
        "else:\n"
        "    print(\"unsaved\")\n"
        "' 2>/dev/null\n"
        ")\" = \"saved\" ]\n"
        "    else\n"
        "        [ \"$(_remember_run_jq -r --arg id \"$1\" \"$SAVED_QUERY\" \"$LAST_SAVE_FILE\" 2>/dev/null)\" = \"saved\" ]\n"
        "    fi\n"
        "}\n"
        "unused() {\n"
        "    echo unused\n"
        "}\n"
        "used\n"
    )
    _out, report = compile_hooks.tree_shake(text)
    assert report["shaken"] is True, report.get("reason")
    assert report["dynamic_dispatch"] is False
    assert report["dropped"] == ["unused"]


@pytest.mark.parametrize("hook_name", compile_hooks.HOOK_SCRIPT_NAMES)
def test_real_hook_tree_shaking_drops_at_least_one_function_and_stays_valid(hook_name):
    # Real-repo positive control for the issue's own ask: each of the four
    # actual compiled hooks must genuinely shrink (at least one function
    # dropped) and must still be syntactically valid bash afterward.
    key = f"scripts/{hook_name}"
    contents = _sh_texts()
    if key not in contents:
        pytest.skip(f"{key} not present in this checkout")
    if is_compiled_text(contents[key]):
        pytest.skip(f"{key} is already compiled (#900 compiled CI leg): there is "
                    f"no source left here to shake, only its already-shaken build")
    text, report = compile_hooks.compile_hook_report(key, contents)
    assert report["shaken"] is True, (
        f"{hook_name}: tree-shaking did not run: {report['reason'] or report}"
    )
    assert len(report["dropped"]) >= 1, f"{hook_name}: no function was dropped"
    result = subprocess.run([_bash(), "-n"], input=text, capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"{hook_name}: shaken output fails bash -n:\n{result.stderr}"


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


# -- round 4: the compiled form must keep `source`'s RUNTIME semantics --------
#
# The compiled-hooks CI leg (tests.yml hook-tests-compiled) found the real
# post-tool-hook.sh no longer resolving PROJECT_DIR on its slow path: the
# first inlining of lib-slug.sh / lib-memory-dir.sh / log.sh sat on the FAST
# branch (or inside the fast path's lazy log() stub), so a build-time include
# guard replaced every later `source` of them with `:` -- including the ones
# on the slow branch, which is the only one that runs on a cold cache. A
# build-time guard cannot know which branch runs; only the library's own
# runtime guard can. Each fixture below runs the SAME text sourced and
# compiled and requires identical output, in both directions of the branch
# (the branch that worked before is the positive control).

def _run_both(tmp_path: Path, files: dict, env_extra: dict) -> tuple:
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (scripts / name).write_text(text, encoding="utf-8")
    contents = {f"scripts/{name}": text for name, text in files.items()}
    compiled_text = compile_hooks.compile_hook("scripts/hook.sh", contents)
    assert compile_hooks.unresolved_sources(compiled_text) == []
    compiled_path = scripts / "hook-compiled.sh"
    compiled_path.write_text(compiled_text, encoding="utf-8")
    env = {**os.environ, "T_ROOT": str(tmp_path), **env_extra}
    runs = []
    for path in (scripts / "hook.sh", compiled_path):
        r = subprocess.run([_bash(), str(path)], env=env, capture_output=True,
                           text=True, check=False)
        runs.append((r.returncode, r.stdout, r.stderr))
    return runs[0], runs[1]


_BRANCH_FILES = {
    "lib-slug.sh": _LIB_SLUG,
    "lib-boot.sh": 'source "${T_ROOT}/scripts/lib-slug.sh"\necho booted\n',
    "hook.sh": (
        '#!/usr/bin/env bash\n'
        'set -u\n'
        'if [ "${MODE:-}" = fast ]; then\n'
        '    source "${T_ROOT}/scripts/lib-slug.sh"\n'
        'else\n'
        '    source "${T_ROOT}/scripts/lib-boot.sh"\n'
        'fi\n'
        'echo "$(slugify x)"\n'
    ),
}


@pytest.mark.parametrize("mode", ["fast", "slow"])
def test_a_library_first_sourced_on_a_branch_not_taken_still_loads(tmp_path, mode):
    # "fast" is the positive control (the branch holding the FIRST source
    # runs, which worked before round 4); "slow" is the shape that broke
    # the real post-tool-hook.sh.
    (src_rc, src_out, src_err), (c_rc, c_out, c_err) = _run_both(
        tmp_path, _BRANCH_FILES, {"MODE": mode})
    assert src_out.strip().endswith("slug-x"), src_err
    assert (c_rc, c_out) == (src_rc, src_out), c_err


_RESOLVE_FILES = {
    "lib-resolve.sh": (
        '[ -n "${T_FAIL:-}" ] && return 1\n'
        'echo resolved\n'
    ),
    "hook.sh": (
        '#!/usr/bin/env bash\n'
        'SOFT=1 source "${T_ROOT}/scripts/lib-resolve.sh" || exit 0\n'
        'echo after\n'
    ),
}


@pytest.mark.parametrize("fail", ["", "1"])
def test_a_librarys_own_return_status_still_reaches_the_caller(tmp_path, fail):
    # resolve-paths.sh's soft-failure mode is exactly this shape: the
    # library `return 1`s and the hook's `|| exit 0` is what stops it. That
    # trailing guard is NOT dead code once inlined -- it is the library's
    # own verdict. fail="" is the positive control (both print "after").
    (src_rc, src_out, src_err), (c_rc, c_out, c_err) = _run_both(
        tmp_path, _RESOLVE_FILES, {"T_FAIL": fail})
    expected = "" if fail else "resolved\nafter\n"
    assert src_out == expected, src_err
    assert (c_rc, c_out) == (src_rc, src_out), c_err
    assert "can only `return'" not in c_err


def test_an_assignment_prefix_on_source_has_the_same_lifetime_compiled(tmp_path):
    # Whatever bash does with `FOO=1 source lib` (temporary for the
    # duration in bash's default mode), the compiled form must do the same
    # -- the lib sees FOO=1 in both, and FOO's value afterwards matches.
    files = {
        "lib.sh": 'echo "inside=${FOO:-unset}"\n',
        "hook.sh": (
            '#!/usr/bin/env bash\n'
            'FOO=1 source "${T_ROOT}/scripts/lib.sh"\n'
            'echo "after=${FOO:-unset}"\n'
        ),
    }
    (src_rc, src_out, src_err), (c_rc, c_out, c_err) = _run_both(tmp_path, files, {})
    assert "inside=1" in src_out, src_err
    assert (c_rc, c_out) == (src_rc, src_out), c_err


def test_compiled_hook_carries_the_generated_marker_and_source_does_not():
    # tests/_compiled_hooks.py keys off this marker to tell a compiled hook
    # (the compiled CI leg compiles in place) from its source -- the
    # source-TEXT pins (#367, #511, #637, #695) describe the source file,
    # which a compiled build no longer is.
    contents = _sh_texts()
    key = "scripts/post-tool-hook.sh"
    if is_compiled_text(contents[key]):
        pytest.skip("this checkout's hooks are already compiled (compiled CI leg)")
    compiled = compile_hooks.compile_hook(key, contents)
    assert compiled.splitlines()[0].startswith("#!")
    assert is_compiled_text(compiled)
    assert not is_compiled_text(contents[key])


# The real hooks, end to end on the COLD path: no env cache (fresh TMPDIR), no
# CLAUDE_PROJECT_DIR, project root from the stdin `cwd`. This is the POSIX
# twin of tests/test_windows_native_hook_cwd_448.py's post-tool case, which is
# where the compiled CI leg first caught the slow-path library loss; there a
# warm env cache left by an earlier test hid it on Linux/macOS. "source" is
# the positive control: the same harness, uncompiled, must resolve.
_PLUGIN_PARTS = (".claude-plugin", "scripts", "pipeline", "hooks", "hooks.d", "prompts")
_PLUGIN_FILES = ("config.example.json", "config.user.example.json", "promos.json",
                 "identity.example.md")


def _plugin_copy(dest: Path, compiled: bool) -> Path:
    for part in _PLUGIN_PARTS:
        if (REPO_ROOT / part).is_dir():
            shutil.copytree(REPO_ROOT / part, dest / part,
                            ignore=shutil.ignore_patterns("__pycache__"))
    for f in _PLUGIN_FILES:
        if (REPO_ROOT / f).is_file():
            shutil.copy2(REPO_ROOT / f, dest / f)
    if compiled:
        assert compile_hooks.main(["--repo", str(dest), "--apply"]) == 0
    return dest


_COLD_PAYLOADS = {
    "post-tool-hook.sh": {"hook_event_name": "PostToolUse", "tool_name": "Read",
                          "tool_input": {"file_path": "/x.py"},
                          "tool_response": {"content": "ok"}},
    "user-prompt-hook.sh": {"hook_event_name": "UserPromptSubmit", "prompt": "hello"},
    "session-start-hook.sh": {"hook_event_name": "SessionStart", "source": "startup"},
}


@pytest.mark.parametrize("compiled", [False, True], ids=["source", "compiled"])
@pytest.mark.parametrize("hook_name", sorted(_COLD_PAYLOADS))
def test_real_hook_resolves_the_project_on_a_cold_cache(tmp_path, hook_name, compiled):
    plugin = _plugin_copy(tmp_path / "plugin", compiled)
    hook = plugin / "scripts" / hook_name
    if not compiled and is_compiled_text(hook.read_text(encoding="utf-8")):
        pytest.skip("this checkout's hooks are already compiled (compiled CI leg) "
                    "-- the 'source' half has no source to run here")
    home = tmp_path / "home"
    home.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    payload = {"session_id": "aaaaaaaa-0000-4000-8000-000000000900",
               "transcript_path": "/does/not/matter.jsonl", "cwd": str(project),
               **_COLD_PAYLOADS[hook_name]}
    env = {**os.environ, "HOME": str(home), "TMPDIR": str(tmp),
           "CLAUDE_PLUGIN_ROOT": str(plugin)}
    for k in ("CLAUDE_PROJECT_DIR", "REMEMBER_HOOK_CWD"):
        env.pop(k, None)
    r = subprocess.run([_bash(), str(hook)], env=env,
                       input=json.dumps(payload), capture_output=True, text=True,
                       timeout=60, cwd=str(project), check=False)
    assert r.returncode == 0, r.stderr
    assert (project / ".remember").is_dir(), (
        f"{hook_name} ({'compiled' if compiled else 'source'}) did not resolve "
        f"PROJECT_DIR from stdin cwd on a cold cache; stderr:\n{r.stderr}")
    assert "command not found" not in r.stderr, r.stderr
    errlog = project / ".remember" / "logs" / "hook-errors.log"
    if errlog.is_file():
        assert "command not found" not in errlog.read_text(encoding="utf-8", errors="replace")
