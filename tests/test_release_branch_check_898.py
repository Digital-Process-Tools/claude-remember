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
    assert not any("<<" in r or "URL host" in r or "network command" in r
                   or "MCP_FORWARDS_CREDENTIAL_ENV" in r or "VAR" in r
                   for r in result.reviews)


# -- typed heredoc (REVIEW, not FAIL -- see _check_typed_heredoc's own docstring) --

def test_a_typed_heredoc_in_a_shipped_script_is_reviewed(tmp_path):
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\ncat << EOF\nhi\nEOF\n"})
    reviews = _check(root).reviews
    assert any("run.sh" in r and ("<" "<") in r for r in reviews), reviews


def test_a_here_string_is_not_a_typed_heredoc(tmp_path):
    """Positive control: `<<<` (a here-string) must not trip the same guard --
    the directory did not flag it in jit-context's own measurement."""
    root = _tree(tmp_path, {"scripts/run.sh": b"#!/bin/sh\ncat <<< hi\n"})
    reviews = _check(root).reviews
    assert not any("run.sh" in r and ("<" "<") in r for r in reviews), reviews


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
