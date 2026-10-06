"""A cloned repository's own `.remember/config.json` must not choose the
summarizer's credential or which variables reach it (#726).

`_configured_oauth_token()` (since removed) / `_configured_drop_env()`
(pipeline/haiku.py) read `haiku.*` from the merged config
`lib-memory-dir.sh` builds, which deep-merges the per-project layer on top
of user-global and bundled -- with no distinction, before this fix, between
"a project I trust wrote this" and "a repository I cloned shipped this
file". In the default (legacy) storage layout, REMEMBER_DIR sits inside the
project checkout, so `.remember/config.json` there is exactly as trustworthy
as any other file the repository ships: not at all, for a repo the operator
did not author.

Every "must not carry through" case here is paired with a "must still carry
through" case in the same fixture shape (a non-haiku key from the same file,
or the same key from a trusted layer), so a merge that dropped the entire
project layer -- or the whole `haiku` key everywhere -- cannot pass as
"fixed" (see CLAUDE.md: a negative assertion needs a positive control).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import _config_candidates, _remember_dir_is_project_local
from tests.test_jq_free_config import _path_without_jq
from tests.test_layered_config import DETECT_SCRIPT, LIB_SCRIPT, _run_lib


def _path_no_jq_broken_drop_marker_mktemp(tmp_path: Path) -> str:
    """#804: a PATH resolving every real binary as-is except `jq` (absent,
    forcing the no-jq Python fallback) and `mktemp` (replaced by a shim
    that fails ONLY for the `*drop-marker*` template lib-memory-dir.sh
    uses for `_project_drop_marker`). This simulates the drop-marker's OWN
    `mktemp` call failing without breaking the file's OTHER `mktemp` calls
    ($_merged_cfg, $_project_sanitized_tmp), which must keep succeeding
    for the harness itself to produce a merged config at all."""
    fake_bin = tmp_path / "no-jq-broken-mktemp-bin"
    fake_bin.mkdir()
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp, "mktemp not found on PATH"
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name in ("jq", "mktemp"):
                continue
            target = fake_bin / name
            if target.exists() or target.is_symlink():
                continue
            try:
                os.symlink(os.path.join(d, name), target)
            except OSError:
                pass
    shim = fake_bin / "mktemp"
    shim.write_text(
        '#!/bin/sh\n'
        'for a in "$@"; do\n'
        '    case "$a" in\n'
        '        *drop-marker*) exit 1 ;;\n'
        '    esac\n'
        'done\n'
        'exec "' + real_mktemp + '" "$@"\n'
    )
    shim.chmod(0o755)
    return str(fake_bin)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX lib-memory-dir.sh — not portable to Windows runners (#79)",
)


def _dirs(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    pipeline = tmp_path / "plugin"
    pipeline.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return project, pipeline, home


def _git(repo: Path, args: list) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _git_init_commit(project: Path, tracked_rel_path: "str | None") -> None:
    """Turn `project` into a real git repo (#757 review: distinguishing
    tracked/untracked/could-not-tell needs an ACTUAL work tree, not just a
    `.git` directory sitting there). When `tracked_rel_path` is given, that
    file is `git add`ed and committed; when it is None, a repo still exists
    but nothing is ever staged -- the "real git repo, file still untracked"
    case, distinct from "no git repo at all"."""
    _git(project, ["init", "-q"])
    _git(project, ["config", "user.email", "t@t"])
    _git(project, ["config", "user.name", "T"])
    if tracked_rel_path is not None:
        _git(project, ["add", tracked_rel_path])
        _git(project, ["commit", "-q", "-m", "seed"])


def _path_with_broken_git(tmp_path: Path) -> str:
    """A PATH where every OTHER real binary resolves normally but `git`
    is a shim that always exits 128 with an unrelated fatal error --
    never "not a git repository", never "did not match any file(s)
    known to git" -- so `_remember_config_tracked_status` cannot read it
    as either `untracked` state and must report `could-not-tell`."""
    fake_bin = tmp_path / "broken-git-bin"
    fake_bin.mkdir()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name == "git":
                continue
            target = fake_bin / name
            if target.exists() or target.is_symlink():
                continue
            try:
                os.symlink(os.path.join(d, name), target)
            except OSError:
                pass
    shim = fake_bin / "git"
    shim.write_text("#!/bin/sh\necho 'fatal: simulated unrelated git failure' >&2\nexit 128\n")
    shim.chmod(0o755)
    return str(fake_bin)


def _path_without_git(tmp_path: Path) -> str:
    """A PATH where `git` has no entry AT ALL -- `command -v git` genuinely
    fails -- distinct from `_path_with_broken_git` above (git present but
    every invocation exits 128). #909: this is the branch that lost its only
    test when #899 deleted the legacy-migration suite; it must walk the
    filesystem by hand and fail CLOSED (`could-not-tell`) whenever an
    enclosing `.git` exists, since no git spawn is possible to ask it
    properly."""
    fake_bin = tmp_path / "no-git-bin"
    fake_bin.mkdir()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if name == "git":
                continue
            target = fake_bin / name
            if target.exists() or target.is_symlink():
                continue
            try:
                os.symlink(os.path.join(d, name), target)
            except OSError:
                pass
    return str(fake_bin)


def _call_tracked_status_directly(check_dir: Path, pipeline_dir: Path, home_dir: Path,
                                   env_extra=None) -> str:
    """Source lib-memory-dir.sh (with a scratch, config-less PROJECT_DIR so
    sourcing itself never needs to call the function or spawn git) and then
    call `_remember_config_tracked_status` directly against `check_dir`,
    returning its literal stdout state (`tracked` / `untracked` /
    `could-not-tell`). Going through the full haiku-config merge instead
    cannot tell `could-not-tell` apart from a GIT-PRESENT `untracked` --
    both strip the same fields -- which is exactly the distinction #909
    needs pinned directly."""
    scratch_project = pipeline_dir / "scratch-project-for-sourcing"
    scratch_project.mkdir(exist_ok=True)
    script = f"""
    set -e
    export PROJECT_DIR={scratch_project}
    export PIPELINE_DIR={pipeline_dir}
    export HOME={home_dir}
    source {DETECT_SCRIPT}
    source {LIB_SCRIPT}
    _remember_config_tracked_status {check_dir} config.json
    """
    env = {**os.environ, **(env_extra or {})}
    result = subprocess.run(["bash", "-c", script], env=env, check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"lib-memory-dir.sh failed:\n{result.stderr}"
    return result.stdout.strip()


def _run_lib_and_dump_config(project_dir, pipeline_dir, home_dir, env_extra=None):
    """Source lib-memory-dir.sh and cat the merged config back INSIDE the
    same script -- REMEMBER_CONFIG is a mktemp file the script's own EXIT
    trap removes the moment the subprocess ends, so reading it from a path
    handed back to the caller (after the process has already exited) finds
    nothing there. Returns the parsed merged config, and REMEMBER_DIR as a
    second value for tests that need to write a config at the resolved
    external-mode path.
    """
    script = f"""
    set -e
    export PROJECT_DIR={project_dir}
    export PIPELINE_DIR={pipeline_dir}
    export HOME={home_dir}
    source {DETECT_SCRIPT}
    source {LIB_SCRIPT}
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    echo "---MERGED---"
    if [ -f "$REMEMBER_CONFIG" ]; then
        cat "$REMEMBER_CONFIG"
    fi
    """
    env = {**os.environ, **(env_extra or {})}
    result = subprocess.run(["bash", "-c", script], env=env, check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"lib-memory-dir.sh failed:\n{result.stderr}"
    remember_dir_line, _, merged_json = result.stdout.partition("---MERGED---\n")
    assert remember_dir_line.startswith("REMEMBER_DIR="), result.stdout
    remember_dir = remember_dir_line.strip().split("=", 1)[1]
    merged = json.loads(merged_json) if merged_json.strip() else {}
    return merged, remember_dir


def _run_lib_and_dump_config_with_stderr(project_dir, pipeline_dir, home_dir, env_extra=None):
    """Same shape as `_run_lib_and_dump_config`, but also returns the
    subprocess's stderr (#748: the sanitize-failure disclosure this file
    adds is a `log`/`report_error` fallback that writes to stderr when
    neither function is in scope yet -- exactly the case here, since this
    helper sources lib-memory-dir.sh directly, never log.sh)."""
    script = f"""
    set -e
    export PROJECT_DIR={project_dir}
    export PIPELINE_DIR={pipeline_dir}
    export HOME={home_dir}
    source {DETECT_SCRIPT}
    source {LIB_SCRIPT}
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    echo "---MERGED---"
    if [ -f "$REMEMBER_CONFIG" ]; then
        cat "$REMEMBER_CONFIG"
    fi
    """
    env = {**os.environ, **(env_extra or {})}
    result = subprocess.run(["bash", "-c", script], env=env, check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"lib-memory-dir.sh failed:\n{result.stderr}"
    remember_dir_line, _, merged_json = result.stdout.partition("---MERGED---\n")
    assert remember_dir_line.startswith("REMEMBER_DIR="), result.stdout
    remember_dir = remember_dir_line.strip().split("=", 1)[1]
    merged = json.loads(merged_json) if merged_json.strip() else {}
    return merged, remember_dir, result.stderr


class TestProjectLocalHaikuConfigIsUntrusted:

    def test_project_haiku_oauth_token_does_not_reach_the_merged_config(self, tmp_path):
        """The attack in #726: a cloned repo's .remember/config.json sets
        haiku.oauth_token -- it must not appear in the merged config at all
        (default legacy storage layout, REMEMBER_DIR inside the project)."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "a" * 40}})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_project_haiku_list_does_not_replace_the_bundled_one(self, tmp_path):
        """A list under `haiku` in an untrusted project file must not replace
        the bundled layer's list in the merge -- the bundled value survives
        untouched. (#898 rounds 15-16 had two such lists; round 17 moved both
        back into code, but the merge rule is generic and stays pinned.)"""
        project, pipeline, home = _dirs(tmp_path)
        bundled = ["CLAUDECODE", "CLAUDE_CODE_SESSION_ID"]
        (pipeline / "config.json").write_text(
            json.dumps({"haiku": {"example_list": bundled}})
        )
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"haiku": {"example_list": []}})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged.get("haiku", {}).get("example_list") == bundled

    def test_project_haiku_removal_does_not_touch_other_project_keys(self, tmp_path):
        """Positive control: a non-haiku key from the SAME untrusted project
        file must still merge exactly as before -- proving the fix removes
        only the `haiku` object, not the whole file's contribution."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({"cooldowns": {"save_seconds": 99}}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({
                "haiku": {"oauth_token": "a" * 40},
                "cooldowns": {"save_seconds": 999},
            })
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["cooldowns"]["save_seconds"] == 999
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_user_global_haiku_oauth_token_still_carries_through(self, tmp_path):
        """Positive control: the SAME key, configured in the TRUSTED
        user-global layer instead of the project layer, must still reach the
        merged config -- a summarizer that stopped reading any credential
        cannot pass as fixed."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "b" * 40}})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["haiku"]["oauth_token"] == "b" * 40

    def test_external_storage_project_layer_haiku_still_trusted(self, tmp_path):
        """Positive control: in external storage mode REMEMBER_DIR resolves
        OUTSIDE any project checkout (an operator's own directory, never
        shipped by a clone), so that layer's haiku block is not the #726
        attack surface and must still carry through."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": str(home / "ext-mem" / "{slug}")})
        )
        ext_dir = home / "ext-mem"
        ext_dir.mkdir(parents=True)

        # First pass just to discover the resolved external REMEMBER_DIR, so
        # the store's own config.json can be written there before the real run.
        _, remember_dir = _run_lib_and_dump_config(project, pipeline, home)
        remember_dir_path = Path(remember_dir)
        remember_dir_path.mkdir(parents=True, exist_ok=True)
        (remember_dir_path / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "c" * 40}})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["haiku"]["oauth_token"] == "c" * 40

    def test_no_jq_fallback_also_strips_untrusted_project_haiku(self, tmp_path):
        """Same attack, jq off PATH -- the Python merge fallback must apply
        the same rule, not silently trust the project layer because it took
        a different code path (#726)."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({
                "haiku": {"oauth_token": "d" * 40},
                "cooldowns": {"save_seconds": 999},
            })
        )

        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": _path_without_jq(tmp_path)},
        )
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})
        # Positive control within the same call: the non-haiku key survives.
        assert merged["cooldowns"]["save_seconds"] == 999


class TestMultiDocumentProjectConfigDoesNotLeakHaiku:
    """#740: `jq -s` slurps EVERY JSON document across ALL source files into
    one flat array with no file-boundary information, so `.[-1] |= del(.haiku)`
    only strips the LAST document of the LAST file. A project `.remember/
    config.json` that ships TWO whitespace-concatenated JSON documents --
    the first carrying a live `haiku` block, the second empty -- has that
    first document survive `reduce .[] as $x ({}; . * $x)` completely
    unstripped: the exact #726 attack, just wrapped in one extra document."""

    def test_multi_document_project_config_first_document_haiku_is_still_stripped(
        self, tmp_path
    ):
        """The #740 reproduction: two JSON documents in the project config,
        `haiku` in the FIRST one -- must not reach the merged config."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "e" * 40}}) + "\n" + json.dumps({})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_multi_document_project_config_second_document_haiku_is_still_stripped(
        self, tmp_path
    ):
        """Same shape, `haiku` in the SECOND (last) document -- this half
        already passed before the fix (it's exactly what `.[-1]` targeted),
        kept here so a fix that changes strategy cannot regress it."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({}) + "\n" + json.dumps({"haiku": {"oauth_token": "f" * 40}})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_multi_document_project_config_non_haiku_keys_still_merge(self, tmp_path):
        """Positive control: a legitimate non-credential key from a
        multi-document project config must still take effect -- a fix that
        refused or dropped the whole project layer on sight of multiple
        documents cannot pass as correct either."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "g" * 40}})
            + "\n"
            + json.dumps({"cooldowns": {"save_seconds": 777}})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["cooldowns"]["save_seconds"] == 777
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_user_global_haiku_still_works_alongside_multi_document_project_config(
        self, tmp_path
    ):
        """Positive control: the trusted user-global layer's own haiku
        credential must still reach the merged config even when the
        untrusted project layer is a multi-document file being stripped."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "h" * 40}})
        )
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "i" * 40}}) + "\n" + json.dumps({})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["haiku"]["oauth_token"] == "h" * 40


class TestPathIdentityDoesNotDependOnAShellString:
    """#740 follow-up: the first fix compared every document's `input_filename`
    against a shell-supplied `--arg proj "$_project_cfg"` string. That works
    when both sides come from the identical bash variable (they always do
    here), but a maintainer flagged it as reasoned-not-observed on platforms
    where jq's own path handling could diverge from the shell's (a
    Windows-native jq.exe, a symlink, a `./`-prefix) -- and this repo's CI
    skips every bash-subprocess test on win32 (#79), so nothing here could
    ever have caught a real divergence. The shipped fix removes the
    comparison entirely: `--slurpfile proj "$_project_cfg"` has jq open the
    untrusted file directly, by its own single file argument, so there is no
    filename STRING left to compare against anything. These tests exercise
    the two constructible-on-this-platform edges rather than the Windows
    case itself, which cannot be reproduced here."""

    def test_project_cfg_reached_through_a_symlinked_directory_still_strips_haiku(
        self, tmp_path
    ):
        """The project's `.remember` directory is reached through a symlink
        (a `./`-prefix and a case-difference are both flavors of the same
        "the shell's string and the tool's own view of the file can differ"
        concern) -- the untrusted haiku block must still be stripped."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        real_remember = tmp_path / "real-remember-store"
        real_remember.mkdir()
        (real_remember / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "j" * 40}})
        )
        (project / ".remember").symlink_to(real_remember, target_is_directory=True)

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_empty_project_cfg_does_not_strip_trusted_user_haiku(self, tmp_path):
        """Regression for the alternative design considered and rejected
        while fixing this: comparing every document's `input_filename`
        against the LAST document's own filename (rather than reading the
        untrusted file directly via `--slurpfile`) silently picks the wrong
        file's name as "the untrusted one" whenever the project config
        exists but is EMPTY (0 documents) -- the untrusted file then
        contributes nothing to the document stream at all, so the "last
        document" actually belongs to the trusted user-global layer, and
        that layer's own legitimate `haiku` block gets stripped by mistake.
        Verified directly with jq before rejecting that design; this test
        pins the shipped `--slurpfile` design against the same shape."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "k" * 40}})
        )
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text("")  # exists, untrusted, EMPTY

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["haiku"]["oauth_token"] == "k" * 40


class TestNoJqFallbackMultiDocumentProjectConfig:
    """#740 follow-up: the no-jq Python merge fallback reads each source file
    with a single `json.load()`, which raises `json.JSONDecodeError` on a
    project config shipping more than one JSON document. That exception is
    uncaught, so it took the WHOLE merge down with it (falling back to
    bundled defaults only, dropping the user-global layer too) rather than
    stripping `haiku` from each of the untrusted file's own documents and
    keeping everything else -- safe (no leak) but not the same fix the jq
    path got. Parses the untrusted file's documents individually instead,
    the same shape as the jq path's per-document strip."""

    def test_no_jq_fallback_strips_haiku_from_every_document_of_a_multi_document_project_config(
        self, tmp_path
    ):
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "l" * 40}}) + "\n" + json.dumps({"cooldowns": {"save_seconds": 555}})
        )

        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": _path_without_jq(tmp_path)},
        )
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})
        # Positive control: the multi-document project file's OTHER key
        # still merges -- a fix that dropped the whole layer on sight of
        # more than one document would pass the assertion above too.
        assert merged["cooldowns"]["save_seconds"] == 555

    def test_no_jq_fallback_multi_document_project_config_first_document_haiku_is_stripped(
        self, tmp_path
    ):
        """Same shape as the jq-path #740 regression: `haiku` in the FIRST
        of two documents, not the second."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"haiku": {"oauth_token": "m" * 40}}) + "\n" + json.dumps({})
        )

        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": _path_without_jq(tmp_path)},
        )
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})


class TestSanitizeStepFailsClosed:
    """#744: PR #744's CI observed the risk #740's `--slurpfile`/`input_filename`
    design was only ever REASONED about, not observed -- on Windows (Git
    Bash + a native jq.exe) the jq invocation itself errored, and the
    `|| cp "$_bundled_cfg" ...` fallback silently dropped EVERY layer
    (project AND the trusted user-global one), with no error surfaced to
    the user at all. The fix drops `--slurpfile` and `input_filename`
    entirely: a separate, plain `jq -c 'del(.haiku)' "$_project_cfg" >
    sanitized_tmp` call (no path comparison, no `/dev/null` placeholder --
    the same shape as the `jq -s` reduce that was ALREADY proven on every
    platform before #740 ever touched this file) sanitizes the untrusted
    layer BEFORE the original reduce ever sees it. If sanitizing itself
    fails, the project layer is dropped entirely (fail CLOSED) rather than
    merged unsanitized -- these tests exercise that specific path."""

    def test_an_unreadable_project_cfg_drops_the_layer_without_losing_the_rest(
        self, tmp_path
    ):
        """The sanitize step can't even open a project config with no read
        permission -- confirms the failure is scoped to just that layer:
        the trusted user-global layer must still merge normally rather than
        the whole thing collapsing to the bundled-only fallback (which
        would ALSO be safe, but is not what "drop the project layer" means,
        and masks a real "could I even find the source of the failure"
        signal a maintainer would want)."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 321}})
        )
        remember = project / ".remember"
        remember.mkdir()
        project_cfg_path = remember / "config.json"
        project_cfg_path.write_text(
            json.dumps({"haiku": {"oauth_token": "n" * 40}})
        )
        project_cfg_path.chmod(0o000)
        try:
            merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        finally:
            project_cfg_path.chmod(0o644)

        # Positive control: the trusted layer's own key still merged --
        # this is not the total bundled-only fallback.
        assert merged["cooldowns"]["save_seconds"] == 321
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_an_unreadable_project_cfg_no_jq_fallback_also_drops_only_that_layer(
        self, tmp_path
    ):
        """Same shape, no-jq Python fallback: `open()` on an unreadable
        project config raises inside `load_documents()` -- must not take
        the whole merge down with it either."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 322}})
        )
        remember = project / ".remember"
        remember.mkdir()
        project_cfg_path = remember / "config.json"
        project_cfg_path.write_text(
            json.dumps({"haiku": {"oauth_token": "o" * 40}})
        )
        project_cfg_path.chmod(0o000)
        try:
            merged, _ = _run_lib_and_dump_config(
                project, pipeline, home,
                env_extra={"PATH": _path_without_jq(tmp_path)},
            )
        finally:
            project_cfg_path.chmod(0o644)

        # Positive control, same shape as the jq-path test above: the
        # trusted layer's own key must still merge -- not the total
        # bundled-only fallback.
        assert merged["cooldowns"]["save_seconds"] == 322

        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_an_unreadable_project_cfg_logs_the_drop(self, tmp_path):
        """#748: the drop above used to happen with nothing logged anywhere
        -- a project config's own settings (e.g. `handoff_mode`) vanished
        with no explanation. Now a WARNING appears on stderr (the `log`/
        `report_error` fallback this helper's own subprocess falls back to,
        since it sources lib-memory-dir.sh directly, never log.sh)."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        project_cfg_path = remember / "config.json"
        project_cfg_path.write_text(
            json.dumps({"haiku": {"oauth_token": "r" * 40}})
        )
        project_cfg_path.chmod(0o000)
        try:
            merged, _, stderr = _run_lib_and_dump_config_with_stderr(project, pipeline, home)
        finally:
            project_cfg_path.chmod(0o644)

        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})
        assert "lib-memory-dir" in stderr, stderr
        assert "project config layer" in stderr, stderr

    def test_a_readable_project_cfg_does_not_log_a_drop_warning(self, tmp_path):
        """Positive control for the test above: an ordinary, readable
        project config with no haiku block at all must NOT trip the
        drop-warning path -- pairing the "must fire" case with the "must
        not fire" one (CLAUDE.md: a negative assertion needs a positive
        control), since a broken harness that emits no stderr at all would
        otherwise also pass a bare `assert "..." not in stderr`."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(json.dumps({"cooldowns": {"save_seconds": 111}}))

        merged, _, stderr = _run_lib_and_dump_config_with_stderr(project, pipeline, home)

        assert merged["cooldowns"]["save_seconds"] == 111
        assert "lib-memory-dir" not in stderr, stderr

    def test_an_unreadable_project_cfg_no_jq_fallback_logs_the_drop(self, tmp_path):
        """Same shape, no-jq Python fallback: the marker-file signal the
        fallback's `except (OSError, ValueError)` branch writes must reach
        the shell as the same WARNING."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        project_cfg_path = remember / "config.json"
        project_cfg_path.write_text(
            json.dumps({"haiku": {"oauth_token": "s" * 40}})
        )
        project_cfg_path.chmod(0o000)
        try:
            merged, _, stderr = _run_lib_and_dump_config_with_stderr(
                project, pipeline, home,
                env_extra={"PATH": _path_without_jq(tmp_path)},
            )
        finally:
            project_cfg_path.chmod(0o644)

        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})
        assert "lib-memory-dir" in stderr, stderr
        assert "project config layer" in stderr, stderr

    def test_mktemp_failing_for_the_drop_marker_itself_still_logs_the_drop(
        self, tmp_path
    ):
        """#804: the no-jq fallback's own drop-marker `mktemp` call can fail
        (a full or read-only $TMPDIR), leaving `$_project_drop_marker` empty
        -- and the Python subprocess's `if drop_marker_path:` guard then
        skips writing the marker even when the untrusted project layer ALSO
        fails to load in that same run, so the drop used to happen with
        nothing logged (the compound case #748 left open). The fix's
        second, independent signal -- the subprocess's own exit code -- does
        not depend on this mktemp succeeding at all, so the WARNING must
        still surface here."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        project_cfg_path = remember / "config.json"
        project_cfg_path.write_text(
            json.dumps({"haiku": {"oauth_token": "t" * 40}})
        )
        project_cfg_path.chmod(0o000)
        try:
            merged, _, stderr = _run_lib_and_dump_config_with_stderr(
                project, pipeline, home,
                env_extra={"PATH": _path_no_jq_broken_drop_marker_mktemp(tmp_path)},
            )
        finally:
            project_cfg_path.chmod(0o644)

        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})
        assert "lib-memory-dir" in stderr, stderr
        assert "project config layer" in stderr, stderr

    def test_mktemp_failing_for_the_drop_marker_does_not_warn_when_nothing_dropped(
        self, tmp_path
    ):
        """Positive control for the test above (CLAUDE.md: a negative
        assertion needs a positive control): the SAME broken-mktemp shim,
        but a readable project config with no `haiku` block at all -- must
        NOT trip the drop-warning path. Proves the shim alone (mktemp
        failing for the drop-marker template specifically) does not itself
        manufacture a false-positive warning, and that the file's OTHER
        mktemp calls still succeed under it -- a broken harness that
        emitted no stderr at all would otherwise also pass a bare
        `assert "..." not in stderr`."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 777}})
        )

        merged, _, stderr = _run_lib_and_dump_config_with_stderr(
            project, pipeline, home,
            env_extra={"PATH": _path_no_jq_broken_drop_marker_mktemp(tmp_path)},
        )

        assert merged["cooldowns"]["save_seconds"] == 777
        assert "lib-memory-dir" not in stderr, stderr

    def test_a_malformed_project_cfg_drops_the_layer_without_losing_the_rest(
        self, tmp_path
    ):
        """The jq path's sanitize call (`jq -c 'del(.haiku)'`) errors on a
        project config that isn't even valid JSON -- same fail-CLOSED shape
        as the unreadable-file test above, different cause. Expected to
        already pass: the sanitize step's own exit code already gates
        whether `_project_sanitized_tmp` gets added to `_jq_merge_sources`."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 323}})
        )
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text('{"haiku": {"oauth_token": "' + "p" * 40)

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)

        assert merged["cooldowns"]["save_seconds"] == 323
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_a_malformed_project_cfg_no_jq_fallback_drops_the_layer_without_losing_the_rest(
        self, tmp_path
    ):
        """Same shape, no-jq Python fallback: `json.JSONDecodeError` (a
        `ValueError` subclass) from `load_documents()`'s own
        `decoder.raw_decode()` call on invalid JSON must be caught the same
        way an `OSError` already is -- catching only `OSError` lets this
        exception propagate, crashing the WHOLE merge down to the
        bundled-only fallback and losing the trusted user-global layer's
        own key too, exactly the regression changelog.d/740.security.md's
        "drops just that layer" claim says does not happen."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 324}})
        )
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text('{"haiku": {"oauth_token": "' + "q" * 40)

        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": _path_without_jq(tmp_path)},
        )

        assert merged["cooldowns"]["save_seconds"] == 324
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_all_layers_absent_or_dropped_does_not_hang_reading_stdin(
        self, tmp_path
    ):
        """jq's own `inputs`/positional-file reading falls back to STDIN when
        given ZERO file arguments (`jq -s '...' ` with an empty `"$@"` is
        `jq -s '...' ` with none) -- reachable here when bundled and user
        configs are both absent AND the untrusted project layer gets
        sanitize-dropped, leaving `_jq_merge_sources` empty. A hook blocked
        on STDIN never returns, so this asserts the run finishes at all
        (the shared 30s subprocess timeout in `_run_lib_and_dump_config`
        turns a hang into a test failure rather than a stalled suite) and
        produces the same `{}` the "no config files at all" branch already
        produces elsewhere in this file."""
        project, pipeline, home = _dirs(tmp_path)
        # No bundled config.json, no home/.remember/config.json -- both
        # absent, not merely empty.
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text('{"haiku": {"oauth_token": "' + "r" * 40)

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)

        assert merged == {}


class TestMalformedTrustedConfigDisclosure:
    """#815: the untrusted-project-layer branch above (TestSanitizeStepFailsClosed)
    already fails closed and discloses the drop via rc 3 / the drop-marker file
    (#748, #804). The no-jq fallback's OTHER branch -- bundled config and
    user-global config, both TRUSTED sources -- had no equivalent: a malformed
    file there raised uncaught, exiting neither 0 nor 3, so the shell's
    bundled-only fallback fired with NO warning at all. "Trusted" means "the
    operator wrote it", not "it parses". Neither test's malformed file is a
    project config: the first test also writes a WELL-FORMED project config
    purely as a discriminator (see its own docstring below -- in this file's
    default legacy layout that layer is the untrusted-haiku source and goes
    through the pre-existing #744 branch, not the trusted-load branch this
    class is about), and the second test has no project config at all."""

    def test_a_malformed_user_global_cfg_no_jq_fallback_warns_and_drops_just_that_layer(
        self, tmp_path
    ):
        """A THIRD source (a well-formed, non-haiku project config) is the
        part that actually discriminates the fix from the bug: `_cfg_sources`
        is ordered bundled, user-global, project, so the malformed
        user-global layer is hit BEFORE the project layer is ever read. Pre-
        fix, the uncaught exception aborts the whole merge subprocess before
        the project layer's own line is reached -- nothing is ever written,
        the shell's bundled-only fallback fires, and `extra_layer` is lost.
        Post-fix, the malformed layer is skipped in place and the loop
        continues on to merge the project layer -- so `extra_layer` survives
        only under the fix. Asserting solely on a value that also lives in
        the bundled config (as an earlier version of this test did) cannot
        tell the two apart, since bundled-only-fallback and
        drop-just-that-layer produce an IDENTICAL merged value whenever
        bundled is the only OTHER source in play."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 111}})
        )
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            '{"cooldowns": {"save_seconds": ' + "9" * 40
        )
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"extra_layer": "still-here"})
        )

        merged, _, stderr = _run_lib_and_dump_config_with_stderr(
            project, pipeline, home,
            env_extra={"PATH": _path_without_jq(tmp_path)},
        )

        # The malformed user-global layer is dropped; the bundled layer
        # beneath it AND the project layer read after it both still apply,
        # rather than everything past the malformed layer being lost to the
        # old "any nonzero rc -> cp bundled over everything" fallback.
        assert merged["cooldowns"]["save_seconds"] == 111
        assert merged["extra_layer"] == "still-here"
        assert "lib-memory-dir" in stderr, stderr
        assert "WARNING" in stderr, stderr

    def test_a_well_formed_user_global_cfg_no_jq_fallback_does_not_warn(
        self, tmp_path
    ):
        """Positive control for the test above (CLAUDE.md: a negative
        assertion needs a positive control): the SAME no-jq PATH, but a
        well-formed user-global config -- must NOT trip the new warning."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(
            json.dumps({"cooldowns": {"save_seconds": 222}})
        )

        merged, _, stderr = _run_lib_and_dump_config_with_stderr(
            project, pipeline, home,
            env_extra={"PATH": _path_without_jq(tmp_path)},
        )

        assert merged["cooldowns"]["save_seconds"] == 222
        assert "lib-memory-dir" not in stderr, stderr


_TRUSTED_DROP_MSG = (
    "sanitizing a trusted config layer failed (unreadable file or malformed "
    "JSON) -- bundled config, user-global config, and project config (when it "
    "is not the untrusted-haiku source) are all reached here, and one of them "
    "was dropped; the remaining layers still applied"
)


class TestDropWarningGoesToReportErrorWhenDefined:
    """#898 (I): the three drop warnings share one helper. Its contract, pinned
    on the trusted-layer drop: with report_error() in scope the message goes
    there, verbatim, tagged lib-memory-dir; without it, the same text goes to
    stderr as one `[lib-memory-dir] WARNING: ...` line."""

    def _run(self, tmp_path, define_report_error):
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text('{"broken": ')
        stub = ('report_error() { printf "REPORTED [%s] %s\\n" "$1" "$2" >&2; }'
                if define_report_error else ":")
        script = f"""
        export PROJECT_DIR={project}
        export PIPELINE_DIR={pipeline}
        export HOME={home}
        {stub}
        source {DETECT_SCRIPT}
        source {LIB_SCRIPT}
        """
        env = {**os.environ, "PATH": _path_without_jq(tmp_path)}
        result = subprocess.run(["bash", "-c", script], env=env, check=False,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stderr.splitlines()

    def test_without_report_error_the_warning_is_one_stderr_line(self, tmp_path):
        lines = self._run(tmp_path, define_report_error=False)
        assert f"[lib-memory-dir] WARNING: {_TRUSTED_DROP_MSG}" in lines, lines
        assert not any(line.startswith("REPORTED") for line in lines), lines

    def test_with_report_error_the_warning_goes_through_it(self, tmp_path):
        lines = self._run(tmp_path, define_report_error=True)
        assert f"REPORTED [lib-memory-dir] {_TRUSTED_DROP_MSG}" in lines, lines
        assert not any("WARNING" in line for line in lines), lines


def test_run_lib_sanity_check_still_works():
    """Sanity: the sibling _run_lib helper this file imports (used only for
    its constants above) is still importable and unbroken."""
    assert callable(_run_lib)


# ── Python-side defense in depth: the raw REMEMBER_DIR/config.json fallback
# `_config_candidates()` uses when REMEMBER_CONFIG is unset (direct python
# use, tests) never went through lib-memory-dir.sh's strip above, so it needs
# its own guard against the same untrusted project layer (#726).


class TestConfigCandidatesSkipsProjectLocalRememberDir:

    def test_project_local_remember_dir_is_detected(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        remember = project / ".remember"
        remember.mkdir(parents=True)
        monkeypatch.setenv("MEMORY_PROJECT_DIR", str(project))
        assert _remember_dir_is_project_local(str(remember)) is True

    def test_external_remember_dir_is_not_project_local(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        external = tmp_path / "home" / ".remember" / "some-slug"
        external.mkdir(parents=True)
        monkeypatch.setenv("MEMORY_PROJECT_DIR", str(project))
        assert _remember_dir_is_project_local(str(external)) is False

    def test_unknown_project_dir_defaults_to_not_project_local(self, tmp_path, monkeypatch):
        """Positive control: MEMORY_PROJECT_DIR unset (direct python use,
        no shell wrapper) must not disable the raw fallback candidate --
        that would break the documented use _config_candidates carves out."""
        monkeypatch.delenv("MEMORY_PROJECT_DIR", raising=False)
        assert _remember_dir_is_project_local(str(tmp_path / ".remember")) is False

    def test_unresolvable_path_fails_safe_as_project_local(self, tmp_path, monkeypatch):
        """MEMORY_PROJECT_DIR IS set (the shell wrapper did run -- not the
        direct-python case above) but realpath() raises -- must default to
        True (exclude the raw candidate) rather than False, since the wrong
        default here silently reopens #726 on whatever rare host hits this."""
        import pipeline.haiku as haiku_module

        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setenv("MEMORY_PROJECT_DIR", str(project))

        def _raise(path):
            raise OSError("simulated resolution failure")

        monkeypatch.setattr(haiku_module.os.path, "realpath", _raise)
        assert _remember_dir_is_project_local(str(project / ".remember")) is True

    def test_config_candidates_omits_project_local_remember_dir(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        remember = project / ".remember"
        remember.mkdir(parents=True)
        monkeypatch.setenv("MEMORY_PROJECT_DIR", str(project))
        monkeypatch.setenv("REMEMBER_DIR", str(remember))
        monkeypatch.delenv("REMEMBER_CONFIG", raising=False)
        candidates = _config_candidates()
        assert str(remember / "config.json") not in candidates

    def test_config_candidates_still_includes_external_remember_dir(self, tmp_path, monkeypatch):
        """Positive control: an external (non-project-local) REMEMBER_DIR is
        untouched by this fix -- its config.json is still a candidate."""
        project = tmp_path / "proj"
        project.mkdir()
        external = tmp_path / "home" / ".remember" / "some-slug"
        external.mkdir(parents=True)
        monkeypatch.setenv("MEMORY_PROJECT_DIR", str(project))
        monkeypatch.setenv("REMEMBER_DIR", str(external))
        monkeypatch.delenv("REMEMBER_CONFIG", raising=False)
        candidates = _config_candidates()
        assert str(external / "config.json") in candidates


class TestUntrustedProjectModelAndRejectPatternAlsoStripped:
    """#757: `model` and `reject_pattern` are stripped from the untrusted
    per-project layer ONLY when config.json is git-TRACKED there -- i.e.
    committed by the REPOSITORY, not merely sitting in the default/legacy
    layout that #726 already distrusts for `haiku`. Unlike a credential,
    neither key can redirect where transcripts go, only which model is
    billed or whether the refusal gate runs at all (`none` disables it;
    any other value is a regex run over model output an attacker fully
    controls) -- severe enough to strip from a file the repository itself
    committed, not severe enough to break every single-user project's own
    untracked per-project override, the way #726 already accepted for
    `haiku`. Every stripped case here is paired with the SAME key, same
    file, left untracked -- still honoured -- so a fix that stripped
    unconditionally cannot pass."""

    def test_git_tracked_project_model_does_not_reach_the_merged_config(self, tmp_path):
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(json.dumps({"model": "attacker-model"}))
        _git_init_commit(project, ".remember/config.json")

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged.get("model") != "attacker-model"

    def test_git_tracked_project_reject_pattern_does_not_reach_the_merged_config(self, tmp_path):
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(json.dumps({"reject_pattern": "none"}))
        _git_init_commit(project, ".remember/config.json")

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged.get("reject_pattern") != "none"

    def test_git_tracked_project_model_removal_does_not_touch_other_project_keys(self, tmp_path):
        """Positive control: a non-stripped key from the SAME tracked
        project file still merges -- proving the fix removes only the
        named keys, not the whole file's contribution."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"model": "attacker-model", "cooldowns": {"save_seconds": 999}})
        )
        _git_init_commit(project, ".remember/config.json")

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["cooldowns"]["save_seconds"] == 999
        assert merged.get("model") != "attacker-model"

    def test_untracked_project_model_still_carries_through(self, tmp_path):
        """Positive control (#757 review finding): an UNTRACKED per-project
        config.json -- the ordinary case, the user's own, never committed
        -- must still have `model` take effect. This is the exact case a
        first version of this fix broke: #726/#740's own `haiku` strip is
        unconditional on LAYOUT alone, but model/reject_pattern are not
        credential-equivalent, so they may not pay that same cost."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(json.dumps({"model": "sonnet"}))
        # A git repo exists, but the file is never `git add`ed -- untracked.
        _git_init_commit(project, None)

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["model"] == "sonnet"

    def test_untracked_project_reject_pattern_still_carries_through(self, tmp_path):
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(json.dumps({"reject_pattern": "^custom"}))
        _git_init_commit(project, None)

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["reject_pattern"] == "^custom"

    def test_no_git_repo_at_all_project_model_still_carries_through(self, tmp_path):
        """Positive control: no `.git` reachable from the project at all --
        the ordinary case for most users -- must not itself be read as
        "untrackable, so strip". Nothing to check means nothing is
        stripped."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(json.dumps({"model": "sonnet"}))

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["model"] == "sonnet"

    def test_user_global_model_still_carries_through(self, tmp_path):
        """Positive control: the SAME key, configured in the TRUSTED
        user-global layer, must still reach the merged config."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(json.dumps({"model": "sonnet"}))

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["model"] == "sonnet"

    def test_user_global_reject_pattern_still_carries_through(self, tmp_path):
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        (home / ".remember").mkdir(parents=True)
        (home / ".remember" / "config.json").write_text(json.dumps({"reject_pattern": "^custom"}))

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["reject_pattern"] == "^custom"

    def test_external_storage_project_layer_model_still_trusted(self, tmp_path):
        """Positive control: in external storage mode the project layer is
        not the #726/#757 attack surface (REMEMBER_DIR resolves outside any
        checkout a clone could ship), so model/reject_pattern set there must
        still carry through -- even if that external directory happens to
        be git-tracked (not the layout #757 is about)."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": str(home / "ext-mem" / "{slug}")})
        )
        ext_dir = home / "ext-mem"
        ext_dir.mkdir(parents=True)

        _, remember_dir = _run_lib_and_dump_config(project, pipeline, home)
        remember_dir_path = Path(remember_dir)
        remember_dir_path.mkdir(parents=True, exist_ok=True)
        (remember_dir_path / "config.json").write_text(
            json.dumps({"model": "sonnet", "reject_pattern": "^custom"})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged["model"] == "sonnet"
        assert merged["reject_pattern"] == "^custom"

    def test_no_jq_fallback_also_strips_tracked_project_model_and_reject_pattern(self, tmp_path):
        """Same tracked-config attack, jq off PATH -- the Python merge
        fallback must apply the same rule."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({
                "model": "attacker-model",
                "reject_pattern": "none",
                "cooldowns": {"save_seconds": 999},
            })
        )
        _git_init_commit(project, ".remember/config.json")

        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": _path_without_jq(tmp_path)},
        )
        assert merged.get("model") != "attacker-model"
        assert merged.get("reject_pattern") != "none"
        assert merged["cooldowns"]["save_seconds"] == 999

    def test_no_jq_fallback_untracked_project_model_still_carries_through(self, tmp_path):
        """Positive control for the no-jq path: untracked stays honoured
        there too."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(json.dumps({"model": "sonnet"}))
        _git_init_commit(project, None)

        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": _path_without_jq(tmp_path)},
        )
        assert merged["model"] == "sonnet"


class TestGitTrackedCheckFailsClosed:
    """#757 review: `_remember_config_tracked_status` must not read a git
    failure that is NOT "not tracked" as "untracked" -- a PATH shim that
    makes every git invocation exit 128 (a corrupted repo, a permissions
    error, an unrelated fatal error) must fail CLOSED: model/reject_pattern
    are stripped, exactly as if the file had been confirmed tracked."""

    def test_a_git_that_always_fails_strips_model_and_reject_pattern(self, tmp_path):
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"model": "attacker-model", "reject_pattern": "none"})
        )
        # A real repo exists (so rev-parse alone would say "yes, a work
        # tree") -- the broken PATH shim below is what must be hit for
        # ls-files, not the absence of a repository.
        _git_init_commit(project, None)

        broken_git_path = _path_with_broken_git(tmp_path)
        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": broken_git_path},
        )
        assert merged.get("model") != "attacker-model"
        assert merged.get("reject_pattern") != "none"


class TestNoGitBinaryTrackedCheckFailsClosed:
    """#909: #899 removed the legacy-migration suite, which was the only
    place `_remember_config_tracked_status` was ever exercised with NO git
    binary on PATH at all (distinct from `TestGitTrackedCheckFailsClosed`
    above, which has git present but failing). The no-git branch has TWO
    outcomes of its own -- `could-not-tell` when an enclosing `.git` is
    found by the filesystem walk, `untracked` when the walk reaches `/`
    without finding one -- and this class pins both directly, plus a
    separate positive control for the git-PRESENT `untracked` path (a
    different branch of the function entirely), so a degenerate no-git
    branch that always answers `could-not-tell` regardless of what the walk
    finds cannot pass by only ever being exercised with a `.git` present."""

    def test_no_git_binary_with_git_dir_present_is_could_not_tell_and_fails_closed(self, tmp_path):
        project, pipeline, home = _dirs(tmp_path)
        # A real repo exists on disk (what the filesystem walk must find),
        # but it is never queried: no git binary is reachable on PATH, so
        # `command -v git` itself must fail before any spawn is attempted.
        _git_init_commit(project, None)

        no_git_path = _path_without_git(tmp_path)
        assert _call_tracked_status_directly(
            project, pipeline, home, env_extra={"PATH": no_git_path},
        ) == "could-not-tell"

        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"model": "attacker-model", "reject_pattern": "none"})
        )
        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": no_git_path},
        )
        assert merged.get("model") != "attacker-model"
        assert merged.get("reject_pattern") != "none"

    def test_no_git_binary_with_no_git_dir_anywhere_is_untracked(self, tmp_path):
        # The no-git branch's OTHER outcome: no git binary reachable, AND
        # the filesystem walk reaches `/` without ever finding a `.git` --
        # nothing to check, same as the ordinary non-repo case. Without
        # this test, a degenerate no-git branch that always answers
        # `could-not-tell` regardless of what the walk finds would pass
        # the whole suite, since the sibling test above never exercises a
        # project with NO enclosing `.git` at all.
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"model": "operator-model", "reject_pattern": "operator-pattern"})
        )
        # Deliberately NOT a git repo at all -- no _git_init_commit call.

        no_git_path = _path_without_git(tmp_path)
        assert _call_tracked_status_directly(
            project, pipeline, home, env_extra={"PATH": no_git_path},
        ) == "untracked"

        merged, _ = _run_lib_and_dump_config(
            project, pipeline, home,
            env_extra={"PATH": no_git_path},
        )
        assert merged.get("model") == "operator-model"
        assert merged.get("reject_pattern") == "operator-pattern"

    def test_git_present_and_file_untracked_is_untracked_positive_control(self, tmp_path):
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"model": "operator-model", "reject_pattern": "operator-pattern"})
        )
        # A real repo, but the config file itself was never committed --
        # the ordinary, common case this whole mechanism must still allow
        # through untouched. This is a DIFFERENT branch of the function
        # entirely (git genuinely present, rev-parse/ls-files path) from
        # the no-git tests above -- it is a control on the whole mechanism,
        # not on the no-git branch's own second outcome.
        _git_init_commit(project, None)

        assert _call_tracked_status_directly(project, pipeline, home) == "untracked"

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged.get("model") == "operator-model"
        assert merged.get("reject_pattern") == "operator-pattern"
