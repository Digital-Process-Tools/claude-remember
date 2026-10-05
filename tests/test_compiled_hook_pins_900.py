"""Two kinds of pin the compiled hook leg turns red for the wrong reason (#900).

Both were found by merging fix/898 into fix/900 and running the full suite
against the compiled hooks, the way the `hook tests (compiled)` CI leg does.
Neither reproduces on fix/900 alone, so each is pinned here directly on a
planted fixture rather than on the hooks of whatever branch this runs on.

1. A function the hook defines but never calls. The compiler drops every
   unreached function (tree-shaking), so a test that extracts that function
   from the hook's text by name finds nothing. `_stdin_cwd` in
   user-prompt-hook.sh is one: nothing calls it, and it survived on fix/900
   only because a trailing comment on a code line named it, which the
   tree-shaker counted as a reference. Once #898 moved that comment onto its
   own line, the compiled hook lost the function and ten #447/#511/#829 pins
   failed with "_stdin_cwd not found". `skip_if_dropped` skips such a pin on
   a compiled hook -- and only there: on source text a missing function is
   still the caller's failure, never a skip.

2. A file-scoped lint over a compiled hook. The #332 sweep collects every
   digits-guarded name in a file, then flags that name in any `$(( ))` of the
   same file without `10#`. A compiled hook is a dozen libraries in one file,
   so a `_now` guarded in one function makes another function's unrelated
   `$(( _now - 10#$_mtime ))` a finding. The plain pytest job sweeps every
   source file on every leg, so the sweep skips a compiled hook.
"""

from __future__ import annotations

import pytest

from tests._compiled_hooks import COMPILED_MARKER, skip_if_dropped
from tests.test_arith_base_lint_332 import _sweep

_BODY = "_kept() {\n    echo kept\n}\n"


def _write(tmp_path, compiled: bool, body: str = _BODY):
    head = "#!/bin/bash\n" + (COMPILED_MARKER + "\n" if compiled else "")
    path = tmp_path / "hook.sh"
    path.write_text(head + body, encoding="utf-8")
    return path


def test_a_compiled_hook_that_dropped_the_function_skips(tmp_path):
    path = _write(tmp_path, compiled=True)
    with pytest.raises(pytest.skip.Exception) as info:
        skip_if_dropped(path, "_gone")
    assert "_gone" in str(info.value)


def test_a_compiled_hook_that_kept_the_function_does_not_skip(tmp_path):
    """Positive control: the skip keys on the function being absent."""
    skip_if_dropped(_write(tmp_path, compiled=True), "_kept")


def test_a_source_hook_missing_the_function_does_not_skip(tmp_path):
    """On source text a missing function is the caller's failure to report."""
    skip_if_dropped(_write(tmp_path, compiled=False), "_gone")


def test_a_function_name_that_is_only_a_prefix_is_not_a_match(tmp_path):
    """`_stdin_cwd_into` surviving must not count as `_stdin_cwd` surviving."""
    path = _write(tmp_path, compiled=True, body="_kept_into() {\n    :\n}\n")
    with pytest.raises(pytest.skip.Exception):
        skip_if_dropped(path, "_kept")


# One function guards its own _now; another reads an unrelated _now, already
# safe because it comes straight from `date +%s`. Separate files in source,
# one file once compiled.
_CROSS_FUNCTION = (
    "_a() {\n"
    "    local _now=$1\n"
    "    [[ \"$_now\" == *[!0-9]* ]] && return 1\n"
    "}\n"
    "_b() {\n"
    "    local _now\n"
    "    _now=$(date +%s)\n"
    "    echo $(( _now - 10#$2 ))\n"
    "}\n"
)


def test_the_arith_sweep_skips_a_compiled_hook(tmp_path):
    assert _sweep([_write(tmp_path, compiled=True, body=_CROSS_FUNCTION)]) == []


def test_the_arith_sweep_still_reads_the_same_text_as_source(tmp_path):
    """Positive control: the planted text IS a finding when it is source, so
    the empty result above is the skip, not a detector that saw nothing."""
    found = _sweep([_write(tmp_path, compiled=False, body=_CROSS_FUNCTION)])
    assert len(found) == 1 and "$_now" in found[0], found
