"""New FAIL/REVIEW guards added to `.github/scripts/check_release_tree.py` for #898,
following measurements in claude-jit-context's directory-validator write-up: a typed
`<<` here-document, a URL host in a comment, a network command name, an env-read +
send-capable pair in a shipped `.md` file, and `$VAR`/`${VAR}` in the release README.

Every "must fail/review" assertion here is paired with a "must pass" one, the same
convention as tests/test_release_branch_check_851.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"

KIB = 1024
BUDGET = {"max_file_bytes": 256 * KIB, "max_files": 512, "max_total_bytes": 3 * KIB * KIB}


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#851)"
    spec = importlib.util.spec_from_file_location("check_release_tree_898", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree_898"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    base = {
        ".claude-plugin/plugin.json": (b'{"name": "x", "description": "d", '
                                       b'"version": "1.0.0", "author": {"name": "a"}}\n'),
        "README.md": ("# x\n\n" + " ".join(["word"] * 40) + "\n").encode(),
        "LICENSE": b"license\n",
        "scripts/run.sh": b"#!/bin/sh\necho hi\n",
        "hooks/hooks.json": json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh"},
        ]}]}}).encode(),
    }
    base.update(files)
    for rel, data in base.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _check(root: Path):
    mod = _load()
    return mod.check_tree(root, dict(BUDGET))


def test_a_clean_tree_passes_with_no_offenders_or_reviews_from_the_new_guards(tmp_path):
    result = _check(_tree(tmp_path, {}))
    assert result.offenders == []
    assert not any("URL host" in r or "network command" in r
                   or "MCP_FORWARDS_CREDENTIAL_ENV" in r or "VAR" in r
                   for r in result.reviews)


# -- typed heredoc (FAIL, not REVIEW -- #900: a maintainer validation of the
# combined fix/898 round 3 + #900 round 1 tree CONFIRMED this as blocking, so
# _check_typed_heredoc moved from reviews to offenders; see that function's
# own updated docstring) --

def test_a_typed_heredoc_in_a_shipped_script_fails(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\ncat << EOF\nhi\nEOF\n"})
    offenders = _check(root).offenders
    assert any("run.sh" in o and ("<" "<") in o for o in offenders), offenders


def test_a_here_string_is_not_a_typed_heredoc(tmp_path):
    """Positive control: `<<<` (a here-string) must not trip the same guard --
    the directory did not flag it in jit-context's own measurement."""
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\ncat <<< hi\n"})
    offenders = _check(root).offenders
    assert not any("run.sh" in o and ("<" "<") in o for o in offenders), offenders


def test_an_arithmetic_left_shift_is_not_a_typed_heredoc(tmp_path):
    """Positive control, #900: `$(( x << 4 ))` is the arithmetic left-shift
    operator, not a here-document -- the portal has never flagged this shape,
    and masking it out (_mask_arithmetic) is what keeps this guard from
    reading every bitshift in a shipped script as UNPINNED_NPX."""
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\nx=$(( 1 << 4 ))\necho \"$x\"\n"})
    offenders = _check(root).offenders
    assert not any("run.sh" in o and ("<" "<") in o for o in offenders), offenders


def test_a_typed_heredoc_mentioned_in_a_comment_does_not_fail(tmp_path):
    """Positive control, #900 round 3: a `#`-comment that quotes `<<'PYEOF'`
    in backticks to EXPLAIN a heredoc rewrite (fix/898's own convention, seen
    at scripts/detect-tools.sh and scripts/lib-memory-dir.sh) is prose, not
    code -- every sibling guard in this file already skips a comment line
    the same way; this one must too."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"# #898: a quoted here-document (`<<'PYEOF'`) is read by the\n"
                           b"# directory's scanner as a typed `<<` it cannot place.\n"
                           b"cat <<< hi\n"),
    })
    offenders = _check(root).offenders
    # the comment lines themselves FAIL as comment-only lines (#900, _check_shell_comments
    # -- the build strips them); what this pins is that the heredoc guard does not.
    assert not any("run.sh" in o and ("<" "<") in o and "comment-only line" not in o
                   for o in offenders), offenders


# -- URL host in a comment of a shipped script (FAIL) ----------------------------

def test_a_url_host_in_a_script_comment_fails(tmp_path):
    root = _tree(tmp_path, {
        "scripts/run.sh": b"#!/bin/sh\n# see github.com/example/example for more\necho hi\n",
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "URL host" in o for o in offenders), offenders


def test_a_url_host_outside_a_comment_does_not_fail_this_guard(tmp_path):
    """Positive control: the same text as code (not a comment) does not trip the
    comment-only guard -- it is a different file shape than what was measured."""
    root = _tree(tmp_path, {
        "scripts/run.sh": b'#!/bin/sh\necho "github.com/example/example"\n',
    })
    offenders = _check(root).offenders
    assert not any("URL host" in o for o in offenders), offenders


def test_a_url_host_in_a_python_comment_also_fails(tmp_path):
    """Review finding: the guard must not be scoped to hooks/hooks.d/scripts
    only -- a shipped `.py` file's `#` comment is just as real."""
    root = _tree(tmp_path, {
        "pipeline/example.py": b"# see github.com/example/example for more\nx = 1\n",
    })
    offenders = _check(root).offenders
    assert any("example.py" in o and "URL host" in o for o in offenders), offenders


def test_a_url_host_comment_line_inside_a_heredoc_body_does_not_fail(tmp_path):
    """#919: a `#`-led line that looks like a URL-host comment is heredoc
    content, not a real shell comment, once it sits inside the body of a
    typed `<<EOF ... EOF` heredoc -- this guard must track heredoc
    boundaries the same way _in_code() already tracks fenced-code state."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"cat <<EOF\n"
                           b"# see github.com/example/example for more\n"
                           b"EOF\n"),
    })
    offenders = _check(root).offenders
    assert not any("URL host" in o for o in offenders), offenders


def test_a_url_host_comment_line_outside_the_heredoc_still_fails(tmp_path):
    """Positive control for the heredoc-boundary test above: the identical
    `#`-led URL-host line, in the same file, outside any heredoc body, must
    still fail -- the fix must skip real heredoc content without silencing
    the guard everywhere else."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"cat <<EOF\n"
                           b"hi\n"
                           b"EOF\n"
                           b"# see github.com/example/example for more\n"),
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "URL host" in o for o in offenders), offenders


def test_a_bitshift_like_line_in_a_python_file_does_not_suppress_a_later_url_host_comment(
    tmp_path,
):
    """Review finding on #919: the heredoc-opener regex (`<< NAME`) is also the
    shape of a bitwise left-shift against a named operand (Python, JS) and a
    C++-style stream-insertion operator -- neither is a heredoc, and Python has
    no heredoc syntax at all. Heredoc-delimiter tracking must therefore be
    scoped to hooks/hooks.d/scripts (the only directories that can hold a real
    one), or this bare bit-shift line would silently disable the URL-host
    guard for the rest of the file."""
    root = _tree(tmp_path, {
        "pipeline/example.py": (b"mask = flag << SOME_CONST\n"
                                 b"# see github.com/example/example for more\n"),
    })
    offenders = _check(root).offenders
    assert any("example.py" in o and "URL host" in o for o in offenders), offenders


def test_a_plain_heredoc_closer_with_leading_whitespace_does_not_close_it(tmp_path):
    """Review finding on #919: an unquoted `<<EOF` heredoc's closing line must be
    the bare delimiter with no leading/trailing whitespace -- real bash does not
    treat an indented look-alike as the terminator. `.strip() == delim` would
    close the heredoc one line early here and then read the real body line that
    follows (still inside the heredoc, per bash) as an ordinary file line."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"cat <<EOF\n"
                           b"  EOF\n"
                           b"# see github.com/example/example for more\n"
                           b"EOF\n"),
    })
    offenders = _check(root).offenders
    assert not any("URL host" in o for o in offenders), offenders


def test_a_dash_heredoc_closer_with_leading_tabs_still_closes_it(tmp_path):
    """Positive control for the test above: `<<-EOF` heredocs DO strip leading
    tabs from the closing line -- the tab-tolerant and whitespace-strict
    closing paths must not collapse into each other."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"cat <<-EOF\n"
                           b"hi\n"
                           b"\tEOF\n"
                           b"# see github.com/example/example for more\n"),
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "URL host" in o for o in offenders), offenders


# -- #981: heredoc-open tracking must skip comment lines and quoted text --------

def test_a_heredoc_opener_mentioned_in_a_comment_does_not_disable_the_url_host_guard(
    tmp_path,
):
    """#981: bash never opens a heredoc from inside a `#` comment. A comment
    line that merely MENTIONS `<<WORD` (explaining a heredoc rewrite, this
    repo's own convention -- see the #900-round-3 test above) must not leave
    the tracker "open" and silently swallow every later URL-host comment in
    the file -- the sibling _check_typed_heredoc already skips comment lines
    for exactly this reason; this guard's own heredoc tracking did not."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"# was rewritten from cat <<EOF to printf (#898)\n"
                           b"echo hi\n"
                           b"# see github.com/example/example for more\n"),
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "URL host" in o for o in offenders), offenders


def test_a_heredoc_opener_inside_an_ordinary_quoted_string_does_not_open_one(tmp_path):
    """#981 addendum: a `<<WORD`-shaped substring sitting inside an ordinary
    quoted string (not a real heredoc opener) must not open the tracker
    either -- same mechanism, same silent swallow, for code rather than a
    comment."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b'echo "see <<EOF in the docs"\n'
                           b"# see github.com/example/example for more\n"),
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "URL host" in o for o in offenders), offenders


def test_a_real_quoted_heredoc_opener_still_opens_one(tmp_path):
    """Positive control for the test above: the quote-awareness fix must not
    break detection of a REAL quoted heredoc opener (`<<'EOF'`) -- only a
    `<<WORD` shape sitting inside an unrelated quoted string earlier on the
    line."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"cat <<'EOF'\n"
                           b"# see github.com/example/example for more\n"
                           b"EOF\n"),
    })
    offenders = _check(root).offenders
    assert not any("URL host" in o for o in offenders), offenders


def test_an_unmatched_apostrophe_before_a_real_opener_does_not_suppress_it(tmp_path):
    """Review finding on #981: an ordinary English contraction ("it's") with
    no closing quote on the line must not be read as an open quote that
    swallows a REAL heredoc opener later on the same line -- a naive
    quote-toggle scan (flip on any quote char, flip back on the next
    matching one) gets this wrong, because the single apostrophe never
    finds a partner and the toggle stays "on" for the rest of the line. The
    opener must still be detected, so the heredoc body below it (including
    its `#`-led URL-host-shaped line, which is heredoc content, not a real
    comment) must not be misread as ordinary file content."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"cat it's_fine <<EOF\n"
                           b"hi\n"
                           b"# see github.com/example/example for more\n"
                           b"EOF\n"),
    })
    offenders = _check(root).offenders
    assert not any("URL host" in o for o in offenders), offenders


def test_an_unclosed_heredoc_opener_is_reported(tmp_path):
    """#981 addendum: an opener that is never closed must be reported rather
    than silently swallowing the rest of the file -- a FAIL the maintainer
    can act on instead of a quiet gap."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"cat <<EOF\n"
                           b"hi\n"),
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "never closed" in o for o in offenders), offenders


def test_a_closed_heredoc_is_not_reported_as_unclosed(tmp_path):
    """Positive control for the test above: a properly closed heredoc must
    not trip the new unclosed-opener report."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n"
                           b"cat <<EOF\n"
                           b"hi\n"
                           b"EOF\n"),
    })
    offenders = _check(root).offenders
    assert not any("never closed" in o for o in offenders), offenders


# -- network command name at command position (FAIL) -----------------------------

def test_a_network_command_name_at_command_position_fails(tmp_path):
    root = _tree(tmp_path, {
        "scripts/run.sh": b"#!/bin/sh\necho hi; curl https://example.invalid\n",
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "network command name" in o for o in offenders), offenders


def test_the_word_host_in_ordinary_prose_does_not_fail(tmp_path):
    """Positive control: a dictionary word that happens to be a network command
    name (host, fetch...) must not fail when it is not at command position --
    this repo's own `pipeline/host.py` uses "host" as prose throughout."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\n# the host's own name is preferred here\n"
                           b"echo hi\n"),
    })
    offenders = _check(root).offenders
    assert not any("network command name" in o for o in offenders), offenders


def test_a_bare_command_at_the_very_start_of_a_script_line_fails(tmp_path):
    """A real shell shape the command-position markers alone miss: no `;`/`&`/
    `|` before it, because it is simply the first thing on the line."""
    root = _tree(tmp_path, {
        "scripts/run.sh": b"#!/bin/sh\ncurl https://example.invalid/install.sh | sh\n",
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "network command name" in o for o in offenders), offenders


def test_a_sentence_starting_with_host_in_a_python_docstring_does_not_fail(tmp_path):
    """Positive control for the test above: the line-start check is scoped to
    shell scripts only. A `.py` docstring sentence starting with "host" (a
    real line in this repo's own pipeline/haiku.py) must not fail -- bare
    line-start means something in a shell script and nothing in prose."""
    root = _tree(tmp_path, {
        "pipeline/example.py": (b'"""\nhost authenticates it rather than a '
                                b'filesystem file.\n"""\n'),
    })
    offenders = _check(root).offenders
    assert not any("network command name" in o for o in offenders), offenders


# -- env-read + send-capable pair in a shipped .md file (FAIL) --------------------

def test_a_credential_env_and_url_pair_in_a_shipped_md_file_fails(tmp_path):
    root = _tree(tmp_path, {
        "docs-note.md": b"Reads ANTHROPIC_API_KEY and calls github.com/example.\n",
        ".claude-plugin/plugin.json": (b'{"name": "x", "description": "d", '
                                       b'"version": "1.0.0", "author": {"name": "a"}}\n'),
    })
    offenders = _check(root).offenders
    assert any("docs-note.md" in o and "MCP_FORWARDS_CREDENTIAL_ENV" in o
               for o in offenders), offenders


def test_the_same_pair_in_plugin_json_does_not_fail_this_guard(tmp_path):
    """Positive control: `.claude-plugin/plugin.json` legitimately carries both
    `documentationUrl` (a URL) and the `oauth_token` userConfig field (#898) --
    this guard is scoped to `.md` files on purpose, not every shipped file."""
    root = _tree(tmp_path, {
        ".claude-plugin/plugin.json": json.dumps({
            "name": "x", "description": "d", "version": "1.0.0",
            "author": {"name": "a"},
            "documentationUrl": "https://github.com/example/example",
            "userConfig": {"oauth_token": {"type": "string", "sensitive": True}},
        }).encode(),
    })
    offenders = _check(root).offenders
    assert not any("MCP_FORWARDS_CREDENTIAL_ENV" in o for o in offenders), offenders


# -- $VAR / ${VAR} in the release README (FAIL) -----------------------------------

def test_a_dollar_var_in_the_readme_fails(tmp_path):
    root = _tree(tmp_path, {
        "README.md": ("# x\n\n" + " ".join(["word"] * 40) + "\n$HOME/foo\n").encode(),
    })
    offenders = _check(root).offenders
    assert any("README.md" in o and "release README" in o for o in offenders), offenders


def test_a_dollar_sign_with_no_variable_name_does_not_fail(tmp_path):
    """Positive control: a literal '$' with no following identifier (e.g. a price)
    is not a variable reference and must not fail."""
    root = _tree(tmp_path, {
        "README.md": ("# x\n\n" + " ".join(["word"] * 40) + "\ncosts $5 a month\n").encode(),
    })
    offenders = _check(root).offenders
    assert not any("release README" in o for o in offenders), offenders


# -- round 2 (#898): scheme literal outside the allowlist (FAIL) -----------------

def test_a_scheme_literal_in_a_shipped_file_fails(tmp_path):
    root = _tree(tmp_path, {
        "docs-note.md": b"# note\n\nurl_display=${url#https://}\n",
    })
    offenders = _check(root).offenders
    assert any("docs-note.md" in o and "scheme literal" in o for o in offenders), offenders


def test_a_scheme_literal_in_plugin_json_is_allowlisted(tmp_path):
    """Positive control: `.claude-plugin/plugin.json`'s own listing URLs are
    required by the directory and must not fail."""
    root = _tree(tmp_path, {
        ".claude-plugin/plugin.json": json.dumps({
            "name": "x", "description": "d", "version": "1.0.0",
            "author": {"name": "a"},
            "documentationUrl": "https://github.com/example/example",
        }).encode(),
    })
    offenders = _check(root).offenders
    assert not any("scheme literal" in o for o in offenders), offenders


# -- round 2 (#898): standalone network word outside the allowlist (FAIL) -------

def test_a_standalone_network_word_outside_the_allowlist_fails(tmp_path):
    root = _tree(tmp_path, {
        "docs-note.md": b"# note\n\nask your host to open a firewall port\n",
    })
    offenders = _check(root).offenders
    assert any("docs-note.md" in o and "network-command word" in o for o in offenders), \
        offenders


def test_the_same_word_in_an_allowlisted_pipeline_file_does_not_fail(tmp_path):
    """Positive control: pipeline/host.py's own architecture vocabulary must
    not fail -- it is on NETWORK_WORD_ALLOWLIST."""
    root = _tree(tmp_path, {
        "pipeline/host.py": b'"""host is this plugin\'s own per-agent abstraction."""\n',
    })
    offenders = _check(root).offenders
    assert not any("network-command word" in o for o in offenders), offenders


# -- round 2 (#898): the dead/typed-'.' SCRIPT_DIR fallback (FAIL) ---------------

def test_a_dot_fallback_fails(tmp_path):
    root = _tree(tmp_path, {
        "scripts/run.sh": (b'#!/bin/sh\n_D="${BASH_SOURCE[0]%/*}"\n'
                           b'[ "$_D" = "${BASH_SOURCE[0]}" ] && _D="."\n'),
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "directory fallback" in o for o in offenders), offenders


def test_a_pwd_fallback_does_not_fail(tmp_path):
    """Positive control: the actual fix (#898) -- $PWD instead of '.'."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b'#!/bin/sh\n_D="${BASH_SOURCE[0]%/*}"\n'
                           b'[ "$_D" = "${BASH_SOURCE[0]}" ] && _D="$PWD"\n'),
    })
    offenders = _check(root).offenders
    assert not any("directory fallback" in o for o in offenders), offenders


# -- round 2 (#898): one hook script naming another by filename (REVIEW) --------

def test_one_hook_script_naming_another_is_reviewed(tmp_path):
    root = _tree(tmp_path, {
        "scripts/post-tool-hook.sh": (b"#!/bin/sh\n"
                                      b"# see session-start-hook.sh for the same guard\n"),
    })
    reviews = _check(root).reviews
    assert any("post-tool-hook.sh" in r and "session-start-hook.sh" in r
               for r in reviews), reviews


def test_a_non_hook_script_naming_a_hook_script_is_not_reviewed(tmp_path):
    """Positive control: scoped to the four hooks.json-registered scripts
    only -- an ordinary lib script naming one of them is not this guard's
    concern (it is covered, if at all, by the FAIL guards above instead)."""
    root = _tree(tmp_path, {
        "scripts/lib-example.sh": b"#!/bin/sh\n# used by session-start-hook.sh\n",
    })
    reviews = _check(root).reviews
    assert not any("lib-example.sh" in r for r in reviews), reviews


# -- round 5 (#898): computed command word, "$VAR ..." (REVIEW) ----------------

def test_a_computed_command_word_is_reviewed(tmp_path):
    root = _tree(tmp_path, {
        "scripts/run.sh": b"#!/bin/sh\nPYTHON=python3\ncd /tmp && $PYTHON -m pipeline.shell\n",
    })
    reviews = _check(root).reviews
    assert any("run.sh" in r and "command word" in r for r in reviews), reviews


def test_a_quoted_variable_argument_is_not_a_computed_command_word(tmp_path):
    """Positive control: an ordinary argument use ("$PYTHON" passed to a real
    command) must not trip this guard -- only the command-word position."""
    root = _tree(tmp_path, {
        "scripts/run.sh": b"#!/bin/sh\nPYTHON=python3\necho \"using $PYTHON\"\n",
    })
    reviews = _check(root).reviews
    assert not any("run.sh" in r and "command word" in r for r in reviews), reviews


# -- round 4/5 (#898): $PWD literal, bare `env`, nested default (FAIL) ---------

def test_pwd_literal_fails(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nX="$PWD/thing"\n'})
    offenders = _check(root).offenders
    assert any("run.sh" in o and "PWD" in o for o in offenders), offenders


def test_pwd_subshell_is_not_a_pwd_literal(tmp_path):
    """Positive control: $(pwd) -- the fix this guard exists to keep in
    place -- must not itself trip the guard."""
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nX="$(pwd)/thing"\n'})
    offenders = _check(root).offenders
    assert not any("run.sh" in o and "PWD" in o for o in offenders), offenders


def test_bare_env_word_fails(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\nenv | grep KEY\n"})
    offenders = _check(root).offenders
    assert any("run.sh" in o and "'env'" in o for o in offenders), offenders


def test_env_as_part_of_a_longer_word_is_not_a_bare_env_word(tmp_path):
    """Positive control: a variable named *environment* or a word ending in
    'env' must not false-match the bare command."""
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nX="$MY_ENV"\nenvironment_check\n'})
    offenders = _check(root).offenders
    assert not any("run.sh" in o and "'env'" in o for o in offenders), offenders


def test_nested_default_expansion_fails(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nX="${FOO:-$BAR}"\n'})
    offenders = _check(root).offenders
    assert any("run.sh" in o and "nested default" in o for o in offenders), offenders


def test_plain_default_expansion_is_not_a_nested_one(tmp_path):
    """Positive control: an ordinary "${X:-literal}" default (round 4's own
    fix shape) must not trip this guard -- only a default whose value is
    itself another expansion."""
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nX="${FOO:-bar}"\n'})
    offenders = _check(root).offenders
    assert not any("run.sh" in o and "nested default" in o for o in offenders), offenders


# -- round 5 (#898): credential-shaped name outside the allowlist (FAIL) ------

def test_credential_shaped_name_outside_allowlist_fails(tmp_path):
    """A credential-shaped name this plugin has no reason to ship (a made-up
    example, never a real one this repo reads) must still be caught."""
    root = _tree(tmp_path, {
        "pipeline/example.py": b'TOKEN = os.environ.get("EXAMPLE_UNRELATED_API_KEY")\n',
    })
    offenders = _check(root).offenders
    assert any("example.py" in o and "EXAMPLE_UNRELATED_API_KEY" in o for o in offenders), offenders


def test_allowlisted_credential_name_is_not_an_offender(tmp_path):
    """Positive control: CLAUDE_CODE_OAUTH_TOKEN -- a real, intentionally
    shipped identifier named in full -- is accepted (#898, round 6: the
    round-5 split of it into two string halves was itself ruled obfuscation
    and reverted). Round 13 took the Anthropic API key's name OFF this list,
    round 16 the Codex one: nothing shipped names either any more."""
    root = _tree(tmp_path, {
        "pipeline/example.py": b'OAUTH_NAME = "CLAUDE_CODE_OAUTH_TOKEN"\n',
    })
    offenders = _check(root).offenders
    assert not any("example.py" in o for o in offenders), offenders


def test_the_parent_session_messaging_handshake_name_is_no_longer_allowlisted(tmp_path):
    """#898 round 18 (maintainer decision): `_without_session_env` no longer
    removes the parent session's CLAUDE_CODE_MESSAGING_TOKEN -- the child
    inherits it, and nothing shipped names it -- so round 17's allowlist
    entries for it are gone, and a shipped file naming it FAILs again like
    any other unlisted credential-shaped name. Positive control in the same
    run: CLAUDE_CODE_OAUTH_TOKEN, still allowlisted, is not an offender."""
    root = _tree(tmp_path, {
        "pipeline/example.py": (
            b'saved = os.environ.pop("CLAUDE_CODE_MESSAGING_TOKEN", None)\n'
        ),
        "pipeline/control.py": b'OAUTH_NAME = "CLAUDE_CODE_OAUTH_TOKEN"\n',
    })
    offenders = _check(root).offenders
    assert not any("control.py" in o for o in offenders), offenders
    assert any("example.py" in o and "CLAUDE_CODE_MESSAGING_TOKEN" in o
               for o in offenders), offenders
    mod = _load()
    assert "CLAUDE_CODE_OAUTH_TOKEN" in mod.CREDENTIAL_NAME_ALLOWLIST, "positive control"
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in mod.CREDENTIAL_NAME_ALLOWLIST
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in mod.CREDENTIAL_FRAGMENT_ALLOWLIST


# -- round 13/16 (#898): the shipped tree names no provider API key (FAIL) ---
#
# Round 13: the Anthropic one. Round 16 (maintainer decision): the Codex one
# too -- the Codex allow-list names no credential (round 16 in config, round
# 17 back in code as literal names), so nothing shipped needs the name.

_API_KEY_NAMES = ("ANTHROPIC_API_KEY", "CODEX_API_KEY")


@pytest.mark.parametrize("name", _API_KEY_NAMES)
@pytest.mark.parametrize("rel, template", [
    ("pipeline/example.py", 'v = os.environ.get("{}", "")\n'),
    ("scripts/run.sh", "#!/bin/sh\n# strips {} first\necho hi\n"),
    ("config.example.json", '{{"haiku": {{"codex_env_allow": ["PATH", "{}"]}}}}\n'),
    ("config.json", '{{"haiku": {{"strip_session_env": ["{}"]}}}}\n'),
    ("notes.md", "Unset {} before running.\n"),
])
def test_the_api_key_name_anywhere_in_the_shipped_tree_fails(tmp_path, name, rel, template):
    """#898 round 13 (maintainer decision): the directory portal held the
    plugin on this name alone (MCP_FORWARDS_CREDENTIAL_ENV), whatever the
    code did with it -- in code, a comment, shipped data or prose. FAIL, not
    REVIEW: one exact string, no false positive to weigh, and a hold.
    Round 16 adds the Codex name, in every file kind the Anthropic one is
    checked in -- shipped config included, where the allow-list now lives."""
    result = _check(_tree(tmp_path, {rel: template.format(name).encode()}))
    hits = [o for o in result.offenders if o.startswith(rel) and name in o]
    assert hits, result.offenders


def test_no_allowlist_accepts_the_codex_api_key_name():
    """#898 round 16: neither the round-5 credential-name allowlist nor the
    round-14 fragment allowlist may carry the Codex name any more -- a
    stale entry would be the first thing to re-admit it. Positive control:
    the real Claude Code credential is still on both."""
    mod = _load()
    assert "CODEX_API_KEY" not in mod.CREDENTIAL_NAME_ALLOWLIST
    assert "CODEX_API_KEY" not in mod.CREDENTIAL_FRAGMENT_ALLOWLIST
    assert "CLAUDE_CODE_OAUTH_TOKEN" in mod.CREDENTIAL_NAME_ALLOWLIST
    assert "CLAUDE_CODE_OAUTH_TOKEN" in mod.CREDENTIAL_FRAGMENT_ALLOWLIST


def test_a_tree_without_the_api_key_names_has_no_such_offender(tmp_path):
    """Positive control: neighbours of the names -- another ANTHROPIC_ or
    CODEX_ variable, the generic options, the words in prose -- are not them."""
    root = _tree(tmp_path, {
        "pipeline/example.py": b'BASE = "ANTHROPIC_BASE_URL"\nHOME_DIR = "CODEX_HOME"\n',
        "config.example.json": b'{"haiku": {"strip_session_env": [], "codex_env_allow": ["PATH", "CODEX_HOME"]}}\n',
        "notes.md": b"An Anthropic API key or a Codex API key set for another tool.\n",
    })
    result = _check(root)
    for name in _API_KEY_NAMES:
        assert not any(name in o for o in result.offenders), result.offenders


# -- round 7 (#898): indirect-name expansion `${!NAME}` (REVIEW) -------------

def test_indirect_expansion_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nx="${!name}"\n'})
    reviews = _check(root).reviews
    assert any("run.sh" in r and "indirect-name" in r for r in reviews), reviews


def test_array_indices_expansion_is_reviewed_too(tmp_path):
    """Round 8: `${!arr[@]}` (an array's index list) is NOT exempt -- the
    portal cited exactly this shape as "reads an environment variable named
    at run time" (claude-directory-publishing portal.md / triggers.md, whose
    rewrite is a counted `for ((i = 0; i < n; i++))`). A round-7 self-review
    narrowed the regex to skip it; that was wrong and is reverted."""
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nfor i in "${!arr[@]}"; do :; done\n'})
    reviews = _check(root).reviews
    assert any("run.sh" in r and "indirect-name" in r for r in reviews), reviews


def test_an_array_subscript_is_not_indirect_expansion(tmp_path):
    """Positive control: `${arr[0]}` carries no `!` at all and must not trip
    this guard."""
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nx="${arr[0]}"\n'})
    reviews = _check(root).reviews
    assert not any("run.sh" in r and "indirect-name" in r for r in reviews), reviews


# -- round 7 (#898): lone-quote close-emit-reopen idiom (REVIEW) -------------

def test_lone_quote_splice_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\necho 'a'\"'\"'b'\n"})
    reviews = _check(root).reviews
    assert any("run.sh" in r and "lone quote" in r for r in reviews), reviews


def test_an_ordinary_quoted_string_is_not_a_lone_quote_splice(tmp_path):
    """Positive control: a plain single-quoted string with no splice must
    not trip this guard."""
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\necho 'hello'\n"})
    reviews = _check(root).reviews
    assert not any("run.sh" in r and "lone quote" in r for r in reviews), reviews


# -- round 7 (#898): two backslashes immediately before a quote (FAIL) ------

def test_backslash_quote_fails(tmp_path):
    root = _tree(tmp_path, {
        "scripts/run.sh": b"#!/bin/sh\nsed 's/\"/\\\\\"/g'\n",
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "two backslashes" in o for o in offenders), offenders


def test_a_single_backslash_before_a_quote_does_not_fail_this_guard(tmp_path):
    """Positive control: exactly one backslash before a quote (the correct,
    already-fixed shape) must not trip this guard."""
    root = _tree(tmp_path, {
        "scripts/run.sh": b'#!/bin/sh\nx="a\\"b"\n',
    })
    offenders = _check(root).offenders
    assert not any("run.sh" in o and "two backslashes" in o for o in offenders), offenders


# -- round 7 (#898): catch-all `*)` case arm inside a loop (FAIL) -----------

def test_catch_all_case_arm_inside_a_while_loop_fails(tmp_path):
    root = _tree(tmp_path, {
        "scripts/run.sh": (b"#!/bin/sh\nwhile read -r x; do\n"
                           b'  case "$x" in\n    a) ;;\n    *) break ;;\n  esac\n'
                           b"done\n"),
    })
    offenders = _check(root).offenders
    assert any("run.sh" in o and "catch-all" in o for o in offenders), offenders


def test_catch_all_case_arm_outside_any_loop_does_not_fail_this_guard(tmp_path):
    """Positive control: the identical case statement, with no enclosing
    loop at all, must not trip this guard -- matching jit-context's own
    measurement that an unlooped catch-all never held."""
    root = _tree(tmp_path, {
        "scripts/run.sh": (b'#!/bin/sh\ncase "$x" in\n    a) ;;\n    *) echo no ;;\nesac\n'),
    })
    offenders = _check(root).offenders
    assert not any("run.sh" in o and "catch-all" in o for o in offenders), offenders


# -- round 7 (#898): lone "." / ".." as a quoted string value (REVIEW) ------

def test_lone_dot_string_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nROOT="."\n'})
    reviews = _check(root).reviews
    assert any("run.sh" in r and "'.'" in r for r in reviews), reviews


def test_single_quoted_lone_dot_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\nROOT='.'\n"})
    reviews = _check(root).reviews
    assert any("run.sh" in r and "'.'" in r for r in reviews), reviews


def test_a_real_path_string_is_not_a_lone_dot(tmp_path):
    """Positive control: an ordinary multi-character quoted path must not
    trip this guard."""
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nROOT="/some/path"\n'})
    reviews = _check(root).reviews
    assert not any("run.sh" in r and "'.'" in r for r in reviews), reviews


# -- round 8 (#898): the sweep tool's two newest needles (REVIEW) ------------
# claude-directory-publishing triggers.md 7 and 8: an escaped quote `\"`
# (held in an awk string on jit-context; the sweep flags it anywhere in a
# shell script) and a `*/*` glob used as a case pattern.

def test_escaped_quote_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\necho "say \\"hi\\""\n'})
    reviews = _check(root).reviews
    assert any("run.sh:2" in r and "escaped quote" in r for r in reviews), reviews


def test_escaped_quote_in_a_parameter_expansion_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nrest=${raw#*\\"cwd\\"}\n'})
    reviews = _check(root).reviews
    assert any("run.sh:2" in r and "escaped quote" in r for r in reviews), reviews


def test_quotes_inside_single_quotes_are_not_an_escaped_quote(tmp_path):
    """Positive control: the documented rewrite -- a single-quoted literal
    carrying the double quotes as plain text -- must not trip this guard."""
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\necho 'say \"hi\"'\n"})
    reviews = _check(root).reviews
    assert not any("run.sh" in r and "escaped quote" in r for r in reviews), reviews


def test_an_escaped_backslash_before_a_quote_is_not_an_escaped_quote(tmp_path):
    """Positive control: `"\\\\"` is one escaped backslash closing the string
    -- the sweep's own lookbehind skips it, and so must this guard."""
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\nbs="\\\\"\n'})
    reviews = _check(root).reviews
    assert not any("run.sh" in r and "escaped quote" in r for r in reviews), reviews


def test_escaped_quote_in_a_python_file_is_not_reviewed(tmp_path):
    """Scoped to `.sh`, like round 7's guards: `"\\""` is ordinary Python."""
    root = _tree(tmp_path, {"scripts/tool.py": b'q = "\\""\n'})
    reviews = _check(root).reviews
    assert not any("tool.py" in r and "escaped quote" in r for r in reviews), reviews


def test_slash_glob_case_pattern_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b'#!/bin/sh\ncase "$0" in */*) echo y ;; esac\n'})
    reviews = _check(root).reviews
    assert any("run.sh:2" in r and "'*/*'" in r for r in reviews), reviews


def test_slash_glob_in_a_joined_case_arm_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": (b'#!/bin/sh\ncase "$x" in\n'
                                               b"    '' | */* | *z) echo y ;;\nesac\n")})
    reviews = _check(root).reviews
    assert any("run.sh:3" in r and "'*/*'" in r for r in reviews), reviews


def test_slash_glob_case_arm_on_its_own_indented_line_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": (b'#!/bin/sh\ncase "$x" in\n'
                                               b"    */*/*) echo y ;;\nesac\n")})
    reviews = _check(root).reviews
    assert any("run.sh:3" in r and "'*/*'" in r for r in reviews), reviews


def test_a_slash_glob_in_a_for_list_is_not_a_case_pattern(tmp_path):
    """Positive control: `*/*` as a pathname glob in a `for` list is not a
    case pattern and must not trip this guard; neither must the rewrite
    (`*yq*`-style arm with no slash)."""
    root = _tree(tmp_path, {"scripts/run.sh": (b'#!/bin/sh\nfor f in dir/*/*.md; do :; done\n'
                                               b'case "$0" in *yq*) echo y ;; esac\n')})
    reviews = _check(root).reviews
    assert not any("run.sh" in r and "'*/*'" in r for r in reviews), reviews


def test_slash_glob_case_in_a_python_file_is_not_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/tool.py": b'm = {"a": "*/*)"}  # x | */*)\n'})
    reviews = _check(root).reviews
    assert not any("tool.py" in r and "'*/*'" in r for r in reviews), reviews


def test_round8_shapes_are_reviewed_in_hooks_d(tmp_path):
    """#898 round 9: the opt-in git backup/restore hooks under hooks.d ship
    in the release tree too, so both round-8 needles must reach them."""
    root = _tree(tmp_path, {"hooks.d/after_save/50-x.sh": (
        b'#!/bin/bash\ncase "$r" in\n    -*|*:*|*/*) r="" ;;\nesac\n'
        b'log "run git -C \\"$ROOT\\" push"\n')})
    reviews = _check(root).reviews
    assert any("50-x.sh:3" in r and "'*/*'" in r for r in reviews), reviews
    assert any("50-x.sh:5" in r and "escaped quote" in r for r in reviews), reviews


# -- round 9 (#898): a quoted literal inside a case pattern (REVIEW) ---------
# claude-directory-publishing triggers.md 9: `*", "*)`, `"no such file"*)`,
# `*'"cwd"'*)`. A quoted VARIABLE in a pattern clears.

def test_quoted_literal_case_arm_on_its_own_indented_line_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": (b'#!/bin/sh\ncase "$x" in\n'
                                               b'    *"not logged in"*|*"bad key"*) echo y ;;\nesac\n')})
    reviews = _check(root).reviews
    assert any("run.sh:3" in r and "quoted literal" in r for r in reviews), reviews


def test_quoted_literal_case_after_in_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": (b"#!/bin/sh\ncase \"$raw\" in *'\"cwd\"'*) ;; *) exit 1 ;; esac\n")})
    reviews = _check(root).reviews
    assert any("run.sh:2" in r and "quoted literal" in r for r in reviews), reviews


def test_quoted_literal_whole_word_case_arm_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": (b'#!/bin/sh\ncase "$P" in\n'
                                               b'    "py -3") py -3 "$@" ;;\nesac\n')})
    reviews = _check(root).reviews
    assert any("run.sh:3" in r and "quoted literal" in r for r in reviews), reviews


def test_quoted_variable_case_arm_and_expansion_test_are_not_reviewed(tmp_path):
    """Positive control: a quoted VARIABLE in a pattern clears on the portal
    (triggers.md 9), and so does the documented rewrite -- an expansion test
    with the literal held in a variable or written bare."""
    root = _tree(tmp_path, {"scripts/run.sh": (
        b'#!/bin/sh\ncase "$x" in\n    *"$NL$v$NL"*) echo y ;;\nesac\n'
        b'[ "${x#*"$lit"}" != "$x" ] && echo y\n'
        b'if [ "${x#*not logged in}" != "$x" ]; then echo y; fi\n'
        b'py\\ -3) : ;;\n')})
    reviews = _check(root).reviews
    assert not any("run.sh" in r and "quoted literal" in r for r in reviews), reviews


def test_quoted_literal_case_in_a_python_file_is_not_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/tool.py": b'    "py -3") \n'})
    reviews = _check(root).reviews
    assert not any("tool.py" in r and "quoted literal" in r for r in reviews), reviews


# #898 round 11 (triggers.md 10): a plain copy of the plugin-root variable,
# `X="$CLAUDE_PLUGIN_ROOT"`, makes the portal list a bare "." under
# COMMAND_SCRIPT_NOT_FOLLOWED; `X="${CLAUDE_PLUGIN_ROOT:-}"` cleared it.

def test_plain_plugin_root_copy_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": (
        b'#!/bin/sh\nA="$CLAUDE_PLUGIN_ROOT"\n'
        b'  local B="${CLAUDE_PLUGIN_ROOT}"\n'
        b'export C="$CLAUDE_PLUGIN_ROOT"; echo x\n')})
    reviews = [r for r in _check(root).reviews if "plugin-root" in r]
    assert any("run.sh:2" in r for r in reviews), reviews
    assert any("run.sh:3" in r for r in reviews), reviews
    assert any("run.sh:4" in r for r in reviews), reviews


def test_defaulted_plugin_root_copy_and_path_use_are_not_reviewed(tmp_path):
    """Negative half; the test above is its positive control on the same
    harness. The rewrite and a path built on the variable both clear."""
    root = _tree(tmp_path, {"scripts/run.sh": (
        b'#!/bin/sh\nA="${CLAUDE_PLUGIN_ROOT:-}"\n'
        b'[ -f "${CLAUDE_PLUGIN_ROOT}/x" ] && echo y\n'
        b'B="$CLAUDE_PLUGIN_ROOT/scripts"\n')})
    reviews = _check(root).reviews
    assert not any("plugin-root" in r for r in reviews), reviews


def test_plain_plugin_root_copy_in_a_python_file_is_not_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/tool.py": b'A="$CLAUDE_PLUGIN_ROOT"\n'})
    reviews = _check(root).reviews
    assert not any("tool.py" in r and "plugin-root" in r for r in reviews), reviews