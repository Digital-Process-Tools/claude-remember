"""scripts/doctor.sh --json -- a machine-readable resolution surface for
external callers (#408).

A `{slug}`-keyed external store's real directory can only be recovered by
reimplementing session_dir_slug (UTF-8-aware, hashes over 200 characters,
folds Windows drive letters) or by asking doctor.sh, which already computes
and prints it in the human report. This is that same computation in a form
another program can consume without vendoring the algorithm.

Three states, not two -- `could_not_resolve` must never render as an absent
key or an empty object, which would be indistinguishable from "nothing to
report" (claude-oss#614's own failure mode, on the other side of the same
gap: it reported "no identity.md" against a directory that was never one of
remember's real config layers, because it had no honest way to say "I could
not resolve this" instead of guessing).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

sys.path.insert(0, str(REPO_ROOT))

from pipeline.slug import session_dir_slug as _slug


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    return home, project, remember


def _run_json(home: Path, project: Path | None, remember: Path,
              extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
        **(extra_env or {}),
    }
    if project is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project)
    else:
        env.pop("CLAUDE_PROJECT_DIR", None)
    return subprocess.run(
        ["bash", str(DOCTOR), "--json"], env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )


def test_json_mode_reports_resolved_directory_and_storage_mode(tmp_path):
    """The happy path this issue exists for: an external caller gets the
    resolved directory and the storage mode without vendoring
    session_dir_slug.
    """
    home, project, remember = _project(tmp_path)

    result = _run_json(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") <= 1, (
        "machine-readable mode printed more than one line -- not a single "
        "JSON object a caller can parse without scraping:\n" + result.stdout
    )
    payload = json.loads(result.stdout)
    assert payload["state"] == "resolved", payload
    assert payload["remember_dir"] == str(remember), payload
    assert payload["storage_mode"] in ("legacy", "external"), payload
    assert isinstance(payload["schema_version"], int), payload


def test_json_mode_names_an_assumed_project_dir_rather_than_hiding_it(tmp_path):
    """CLAUDE_PROJECT_DIR unset (the marketplace/Bash-tool gap #207 already
    documents for the human report) must not silently present a guessed
    directory as though Claude Code had supplied it -- the caller needs to
    know the resolution rests on an assumption.
    """
    home, _project_dir, remember = _project(tmp_path)

    result = _run_json(home, None, remember)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "resolved_assumed_project_dir", payload
    assert "remember_dir" in payload, payload


def test_json_mode_escapes_control_bytes_other_than_newline(tmp_path):
    """A resolved path is not guaranteed to be free of raw control bytes
    (a tab, a carriage return) just because it is unusual -- POSIX
    filesystems allow them. `_json_escape()` handled `\\`, `"` and `\\n`
    but passed every other C0 control byte through unescaped, which is
    invalid inside a JSON string literal (RFC 8259) and breaks any real
    parser a caller points at this output -- exactly the population this
    surface exists to serve.
    """
    home, _project_dir, _remember = _project(tmp_path)
    odd_project = tmp_path / "project-with-a-tab-\there"
    odd_remember = odd_project / ".remember"
    (odd_remember / "tmp").mkdir(parents=True)

    result = _run_json(home, odd_project, odd_remember)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "resolved", payload
    # json.loads succeeding is the real assertion -- a raw control byte in
    # the string literal is what json.decoder.JSONDecodeError caught before
    # this fix (reproduced separately as the red step). The fix flattens
    # every control byte to a space (the same lossy-but-valid choice this
    # function already made for a literal newline before this fix), so
    # "tab-here" survives as "tab- here" -- word-separated, not silently
    # dropped, which is what a fix that stripped the byte instead of
    # encoding it would also produce as EMPTY space (no separator at all).
    assert "tab- here" in payload["project_dir"], (
        "the tab was dropped rather than replaced with a separator -- this "
        "assertion is what tells that apart from merely deleting the byte:\n"
        + result.stdout
    )


def test_json_mode_reports_could_not_resolve_rather_than_an_empty_object(tmp_path):
    """The third state: resolution genuinely fails (REMEMBER_NESTED_SUMMARIZER
    forces resolve-paths.sh's own soft-fail path, the same one the human
    report's "FAIL Path resolution failed" arm reads). The caller must get an
    explicit state and a reason, never an empty or absent-key payload that
    reads exactly like "nothing to report" -- the failure mode this issue's
    own claude-oss#614 citation hit on the other side of this same gap.
    """
    home, project, remember = _project(tmp_path)

    result = _run_json(
        home, project, remember,
        {"REMEMBER_NESTED_SUMMARIZER": "1"},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "could_not_resolve", payload
    assert "remember_dir" not in payload, (
        "a failed resolution still reported a directory:\n" + result.stdout
    )
    assert payload.get("reason"), (
        "could_not_resolve carried no reason a caller could act on:\n"
        + result.stdout
    )
