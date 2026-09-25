"""v0.34.1 release-audit hardening of the #757 migration code
(scripts/bootstrap-dirs.sh + scripts/lib-memory-dir.sh).

Four defects, each with a failing-first regression here, plus the positive
controls CLAUDE.md requires for every "must not happen" case:

* a git-TRACKED legacy `.remember/config.json` that is a SYMLINK to a file
  outside the repository must never have its target's bytes copied into
  the working tree, and the link itself must survive untouched;
* the holdout restore (moving a held-out config.json back into place after
  migration) is CHECKED -- a failed restore must never delete the
  operator's only remaining copy, and must log loudly;
* `.Remember/config.json` (differently cased) committed by the repository
  must still be seen as tracked on a case-insensitive filesystem;
* a PATH with no `git` binary at all, inside a real repository, must
  report `could-not-tell` and fail CLOSED -- not the `untracked` a missing
  binary used to produce (#766).

Every "must not migrate/leak" case is paired with a "must still migrate"
positive control, per CLAUDE.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.test_migration import (
    _BASH,
    BOOTSTRAP_SCRIPT,
    DETECT_SCRIPT,
    _bash_path,
    _make_legacy_dir,
)
from tests.test_migration_trust_757 import (
    _git,
    _init_git,
    _run_bootstrap_and_dump_merged_config,
    _source_bootstrap_with_env,
)

pytestmark = pytest.mark.skipif(_BASH is None, reason="Git Bash not found (Windows without Git for Windows)")


def _path_without_git(tmp_path: Path) -> str:
    """PATH with git made genuinely UNREACHABLE (`command -v git` must
    fail -- not merely `git` itself failing when invoked, which is what
    `_path_with_broken_git`, a scenario #766 does not test, already
    covers), while every OTHER tool this script actually calls stays
    reachable exactly as it already was.

    The previous shape of this helper rebuilt a whole fake bin directory
    by symlinking every top-level entry of every original PATH directory
    in, skipping only literal "git". On a windows-latest Git-Bash leg
    that broke a Chocolatey-installed `jq` shim -- a tiny launcher stub
    that locates the REAL jq.exe relative to its own installed location,
    which a symlink elsewhere breaks -- so the broken shim's own error
    text ("Cannot find ... jq.exe ...") landed on stdout where jq's real
    output was expected, and from there into REMEMBER_DIR/config.json.
    Nothing this test's own assertions depend on jq (or python3, lib-
    memory-dir.sh's own jq-missing fallback) being present at all -- both
    are already exercised elsewhere -- so this stops trying to preserve
    anything it does not explicitly name: it DROPS any PATH directory
    that contains a `git`/`git.exe` executable (on this Windows runner,
    git and coreutils like `mv` can live in the SAME directory --
    `_path_with_mv_that_refuses_config_restore` hit the same fact from
    the other side) rather than trying to sift git back out of it file by
    file, then PREPENDS one small shim directory holding only the
    SPECIFIC external tools bootstrap-dirs.sh, detect-tools.sh and
    lib-memory-dir.sh are known to call (mkdir, mv, cp, rm, mktemp, find,
    dirname, grep, sed -- the last two are the no-jq data_dir fallback,
    which runs regardless of whether jq itself is preserved), PLUS `bash`
    itself -- the script-under-test is invoked VIA bash (subprocess), and
    on a ubuntu-latest runner `git` and `bash` live in the SAME PATH
    directory (typically /usr/bin), so dropping that directory to hide
    git used to drop bash along with it, making subprocess itself fail
    to start with `FileNotFoundError: ... 'bash'` before this test class
    could exercise anything about missing git (PR #768) -- resolved to
    their real absolute paths BEFORE any directory is dropped, so a tool
    that happened to live alongside git is not lost along with it, and
    nothing else on PATH is touched."""
    needed = ("mkdir", "mv", "cp", "rm", "mktemp", "find", "dirname", "grep", "sed", "bash")
    resolved = [shutil.which(name) for name in needed]

    orig_dirs = os.environ.get("PATH", "").split(os.pathsep)
    git_dirs = {
        d for d in orig_dirs
        if any((Path(d) / n).exists() for n in ("git", "git.exe"))
    }
    kept_dirs = [d for d in orig_dirs if d not in git_dirs]

    fake_bin = tmp_path / "no-git-bin"
    fake_bin.mkdir()
    for path in resolved:
        if not path or str(Path(path).parent) not in git_dirs:
            continue  # not lost -- still reachable via kept_dirs, no shim needed
        target = fake_bin / Path(path).name
        if target.exists() or target.is_symlink():
            continue
        try:
            os.symlink(path, target)
        except OSError:
            try:
                shutil.copy2(path, target)
            except OSError:
                pass

    return os.pathsep.join([str(fake_bin), *kept_dirs])


def test_path_without_git_keeps_bash_reachable_when_colocated_with_git(tmp_path, monkeypatch):
    """Repro for the PR #768 CI failure (job #108216073091 and 7 sibling
    legs, all red on the same commit): on ubuntu-latest, `git` and `bash`
    live in the SAME PATH directory (typically `/usr/bin`), so dropping
    that whole directory to hide `git` also dropped `bash` -- and the
    script-under-test is invoked VIA `bash` (subprocess), so subprocess
    itself could not even start:
    `FileNotFoundError: [Errno 2] No such file or directory: 'bash'`,
    never reaching anything this test class actually means to exercise.

    Builds a synthetic PATH directory holding both a `git` and a `bash`
    shim -- mirroring the real ubuntu-latest layout regardless of whether
    this dev machine happens to share it -- so the repro does not depend
    on the local platform's own PATH layout, per CLAUDE.md's "reasoned vs
    observed" split for cross-platform claims."""
    real_git = shutil.which("git")
    real_bash = shutil.which("bash")
    assert real_git and real_bash, "need a real git and bash on PATH to build the repro"

    colocated = tmp_path / "colocated-bin"
    colocated.mkdir()
    os.symlink(real_git, colocated / "git")
    os.symlink(real_bash, colocated / "bash")

    monkeypatch.setenv("PATH", str(colocated))

    no_git_path = _path_without_git(tmp_path)

    assert shutil.which("git", path=no_git_path) is None, (
        "git must still be unreachable after _path_without_git -- if this "
        "fails, the helper stopped hiding git at all"
    )
    assert shutil.which("bash", path=no_git_path) is not None, (
        "bash was dropped along with git's directory -- this is the exact "
        "cause of 'FileNotFoundError: No such file or directory: bash' on "
        "PR #768's ubuntu-latest CI legs, because git and bash live in the "
        "same PATH directory there"
    )


def _path_with_mv_that_refuses_config_restore(tmp_path: Path) -> str:
    """The FULL current PATH, with one shim directory PREPENDED, holding
    ONLY an `mv` override that fails when its destination ends in
    `.remember/config.json` -- the config-restore write, and only that
    write. The holdout-creation `mv` (destination is a mktemp path) and the
    whole-directory migration `mv` (destination is REMEMBER_DIR, not this
    literal suffix) are both untouched, so this simulates exactly "the
    destination is unwritable" for the restore step alone.

    Rebuilding a whole fake bin directory by symlinking every OTHER real
    binary in (the previous shape of this helper) silently dropped `git`
    on a windows-latest Git-Bash leg: `mv`(`.exe`) and `git`(`.exe`) can
    live in the SAME PATH directory there, and the exclusion this helper
    used to apply (`if name == "mv": continue`, a bare-name compare) never
    matched Windows' `mv.exe`, so nothing about that shape was actually
    excluding `mv` on that platform in the first place -- yet `git` still
    went missing, because rebuilding PATH from a `os.listdir()` walk of
    each original directory's TOP LEVEL misses however git for Windows
    actually resolves its own supporting files. PREPENDING one single-file
    shim directory to the untouched, real PATH sidesteps all of that: every
    real binary -- git included, wherever and however it is actually laid
    out on this platform -- stays exactly as reachable as it already was;
    only `mv` resolves to the shim first, because shells resolve PATH
    left-to-right."""
    real_mv = shutil.which("mv")
    assert real_mv is not None, "no real mv on PATH to wrap"
    fake_bin = tmp_path / "restore-fails-bin"
    fake_bin.mkdir()
    shim = fake_bin / "mv"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "    */.remember/config.json)\n"
        "        echo 'simulated: destination unwritable' >&2\n"
        "        exit 1\n"
        "        ;;\n"
        "    *)\n"
        f'        exec "{real_mv}" "$@"\n'
        "        ;;\n"
        "esac\n"
    )
    shim.chmod(0o755)
    return str(fake_bin) + os.pathsep + os.environ.get("PATH", "")


def _fs_is_case_insensitive() -> bool:
    """Never touches a hardcoded path -- `tempfile.mkdtemp()` resolves the
    real platform temp root (`%TEMP%` on Windows, `$TMPDIR`/`/tmp` on POSIX),
    so this is safe to call from a `skipif` CONDITION, which pytest
    evaluates at COLLECTION time. A hardcoded `/tmp` here previously crashed
    collection outright on windows-latest (`FileNotFoundError: [WinError 3]
    ... '\\tmp\\case_probe'`) -- there is no `/tmp` on Windows, and nothing
    catches an exception raised while a class-level `pytest.mark.skipif`
    condition is being evaluated; it aborts the whole collection, not just
    this one skip decision. Any failure here (no writable temp directory
    reachable at all) is read the SAME as case-sensitive -- the tests this
    guards are the case-insensitive-only checks, so failing to determine the
    answer must never masquerade as "yes, run them"."""
    probe_dir = None
    try:
        probe_dir = tempfile.mkdtemp(prefix="remember-case-probe-")
        probe = Path(probe_dir)
        (probe / "AbC").write_text("x")
        return (probe / "abc").exists()
    except OSError:
        return False
    finally:
        if probe_dir is not None:
            shutil.rmtree(probe_dir, ignore_errors=True)


def _symlink_targets(link: Path, expected_target: Path) -> bool:
    """Whether a symlink's recorded target resolves to the SAME file as
    `expected_target` -- tolerant of Windows' `os.readlink()` returning
    the `\\\\?\\C:\\...` extended-length form rather than the plain
    path a symlink was created with, which made a bare string-equality
    compare against `str(expected_target)` fail there even though the
    link was correct. `os.path.samefile` resolves both sides through the
    filesystem (device+inode / Windows file ID) instead of comparing
    text, so the prefix form -- and a drive-letter case difference --
    never matters. Requires both paths to exist; a dangling link (this
    module never creates one) would need a different check."""
    try:
        return os.path.samefile(link, expected_target)
    except OSError:
        return False


class TestSymlinkedTrackedConfigNeverFollowed:
    """F1: a git-tracked legacy config.json that is a SYMLINK to a file
    OUTSIDE the repository must never be `cp`'d through -- that would land
    the target's bytes as an ordinary regular file inside the working
    tree, ready to be committed."""

    def test_symlinked_tracked_config_target_bytes_never_land_in_the_tree(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        secret = tmp_path / "outside-repo-secret.txt"
        secret.write_text("super-secret-credential-material-0000000000\n")

        _make_legacy_dir(project)
        legacy = project / ".remember"
        os.symlink(secret, legacy / "config.json")
        _init_git(project)
        _git(project, ["add", "-A"])
        _git(project, ["commit", "-q", "-m", "seed with symlinked config"])

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        merged, remember_dir, stderr = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )

        # The link itself must survive, unchanged, still a symlink to the
        # same target.
        cfg_after = legacy / "config.json"
        assert cfg_after.is_symlink(), "the symlink itself was replaced or removed"
        assert _symlink_targets(cfg_after, secret), "the symlink does not resolve to the expected target"

        # No file anywhere under REMEMBER_DIR holds the secret's bytes.
        remember_root = Path(remember_dir)
        if remember_root.exists():
            for p in remember_root.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    assert secret.read_text() not in p.read_text(errors="ignore"), (
                        f"the symlink target's bytes reached {p} -- the "
                        "symlink was followed during migration"
                    )
        assert "secret" not in json.dumps(merged), "merged config picked up the symlink target"
        assert "757" in stderr

    def test_untracked_regular_config_still_migrates(self, tmp_path):
        """Positive control: an ORDINARY untracked config.json (not a
        symlink) must still migrate normally -- a fix that stopped every
        config.json from migrating cannot pass."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "plain-token"}}))
        _init_git(project)
        # Deliberately never `git add`ed.

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        _run_bootstrap_and_dump_merged_config(str(project), str(pipeline), str(home))
        merged, remember_dir, _ = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )
        assert not (legacy / "config.json").exists()
        assert (Path(remember_dir) / "config.json").exists()
        assert merged["haiku"]["oauth_token"] == "plain-token"


class TestRestoreIsCheckedNeverSilentlyDropped:
    """F6: the holdout restore `mv` used to run unchecked, and the holdout
    was deleted UNCONDITIONALLY afterward -- a failed restore silently lost
    the operator's config with no trace and no message."""

    def test_a_restore_that_fails_does_not_lose_the_config(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)
        sys_tmp = tmp_path / "systmp"
        sys_tmp.mkdir()

        _make_legacy_dir(project)
        legacy = project / ".remember"
        secret_marker = "restore-failure-canary-0000000000000000"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": secret_marker}}))
        _init_git(project)
        _git(project, ["add", ".remember/config.json"])
        _git(project, ["commit", "-q", "-m", "seed"])

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        restore_fails_path = _path_with_mv_that_refuses_config_restore(tmp_path)
        script = f"""
        set -e
        export PROJECT_DIR="{_bash_path(project)}"
        export PIPELINE_DIR="{_bash_path(pipeline)}"
        export HOME="{_bash_path(home)}"
        export TMPDIR="{_bash_path(sys_tmp)}"
        source "{_bash_path(DETECT_SCRIPT)}"
        source "{_bash_path(BOOTSTRAP_SCRIPT)}"
        echo "REMEMBER_DIR=$REMEMBER_DIR"
        """
        env = {**os.environ, "PATH": restore_fails_path}
        result = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, env=env, check=False)
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"

        assert "FAILED to restore" in result.stderr, (
            "a restore failure must be logged loudly, not silently swallowed"
        )

        # The config's content must still exist SOMEWHERE reachable -- not
        # deleted, whichever path it landed on.
        found = False
        for candidate in list(sys_tmp.rglob("remember-legacy-cfg-*")) + [legacy / "config.json"]:
            if candidate.is_file() and secret_marker in candidate.read_text():
                found = True
                break
        assert found, "the config's content is nowhere to be found after a failed restore -- it was lost"

    def test_a_restore_that_succeeds_leaves_no_stray_holdout(self, tmp_path):
        """Positive control: the ordinary (working `mv`) case still leaves
        config.json back in the legacy dir and cleans up after itself,
        exactly as before this hardening."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "tok"}}))
        _init_git(project)
        _git(project, ["add", ".remember/config.json"])
        _git(project, ["commit", "-q", "-m", "seed"])

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        _run_bootstrap_and_dump_merged_config(str(project), str(pipeline), str(home))
        assert (legacy / "config.json").exists()
        assert json.loads((legacy / "config.json").read_text())["haiku"]["oauth_token"] == "tok"


@pytest.mark.skipif(
    not _fs_is_case_insensitive(),
    reason="case-insensitive-filesystem-only check (macOS APFS default); this filesystem is case-sensitive "
    "(or the probe itself could not run -- treated the same way, fail toward skipping this class rather "
    "than asserting a platform behaviour that was never confirmed)",
)
class TestCaseVariantTrackedConfigIsStillSeenAsTracked:
    """F3: `.Remember/config.json` (capital R), committed by the repository,
    must still be reported as TRACKED on a case-insensitive filesystem --
    the exact-case `ls-files --error-unmatch` used to miss it entirely."""

    def test_differently_cased_tracked_config_is_left_behind(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        cased = project / ".Remember"
        (cased / "tmp").mkdir(parents=True)
        (cased / "logs").mkdir(parents=True)
        (cased / "now.md").write_text("## 10:00 | master\nSome work.\n")
        (cased / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "cased-token"}}))

        _init_git(project)
        _git(project, ["add", ".Remember/config.json"])
        _git(project, ["commit", "-q", "-m", "seed with cased dir"])

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        merged, remember_dir, _stderr = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )

        legacy = project / ".remember"  # same on-disk entry as .Remember here
        assert (legacy / "config.json").exists(), (
            "a config.json committed under a DIFFERENTLY CASED path was "
            "read as untracked and migrated as trusted"
        )
        assert not (Path(remember_dir) / "config.json").exists()
        assert "haiku" not in merged or "oauth_token" not in merged.get("haiku", {})

    def test_untracked_cased_config_still_migrates(self, tmp_path):
        """Positive control: same cased directory, file never committed --
        must still migrate normally."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        cased = project / ".Remember"
        (cased / "tmp").mkdir(parents=True)
        (cased / "logs").mkdir(parents=True)
        (cased / "now.md").write_text("## 10:00 | master\nSome work.\n")
        (cased / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "cased-token"}}))

        _init_git(project)
        # Deliberately never `git add`ed.

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        _run_bootstrap_and_dump_merged_config(str(project), str(pipeline), str(home))
        merged, remember_dir, _ = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )
        legacy = project / ".remember"
        assert not (legacy / "config.json").exists()
        assert (Path(remember_dir) / "config.json").exists()
        assert merged["haiku"]["oauth_token"] == "cased-token"


class TestMissingGitFailsClosedInsteadOfTrusting:
    """#766 (public): "no git binary" used to read the SAME as "no repo
    here", trusting the config -- contradicting the documented fail-closed
    behaviour. Missing git INSIDE a real repository must be `could-not-tell`
    and fail closed."""

    def test_git_missing_inside_a_repo_leaves_the_config_behind(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "no-git-token"}}))
        # A real repo exists on disk -- .git is discoverable by WALKING,
        # which is all this can do with no git binary to spawn.
        _init_git(project)
        _git(project, ["add", ".remember/config.json"])
        _git(project, ["commit", "-q", "-m", "seed"])

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        no_git_path = _path_without_git(tmp_path)
        result = _source_bootstrap_with_env(str(project), str(pipeline), str(home), path_override=no_git_path)
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
        remember_dir = result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()

        assert (legacy / "config.json").exists(), (
            "a MISSING git binary, inside a real repository, must fail "
            "CLOSED (could-not-tell) -- it must not read the same as "
            "'there is no repository here' and let the config migrate as "
            "trusted"
        )
        assert not (Path(remember_dir) / "config.json").exists()

    def test_git_missing_with_no_repo_at_all_still_migrates(self, tmp_path):
        """Positive control: no git binary AND no repository anywhere above
        the project dir -- genuinely nothing to check, must still migrate
        normally, exactly like the ordinary no-git case always has."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)
        legacy = project / ".remember"
        (legacy / "config.json").write_text(json.dumps({"haiku": {"oauth_token": "no-repo-token"}}))
        # No `git init` anywhere -- genuinely no repository.

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        no_git_path = _path_without_git(tmp_path)
        result = _source_bootstrap_with_env(str(project), str(pipeline), str(home), path_override=no_git_path)
        assert result.returncode == 0, f"bootstrap failed:\n{result.stderr}"
        remember_dir = result.stdout.strip().split("REMEMBER_DIR=")[-1].strip()

        assert not (legacy / "config.json").exists()
        assert (Path(remember_dir) / "config.json").exists()


class TestSymlinkedProjectConfigNeverReadDuringMerge:
    """F1 (second half): lib-memory-dir.sh's own merge must never open a
    SYMLINKED `${REMEMBER_DIR}/config.json` through the link either -- not
    just bootstrap-dirs.sh's migration holdout. Sets up an external store
    that already exists (no migration involved) with config.json replaced
    by a symlink to a file outside it, then checks the SECOND session's
    merge never picks up the target's content."""

    def test_symlinked_external_config_is_refused_not_read(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        # First session: creates the external store (no legacy dir at all,
        # nothing to migrate) so REMEMBER_DIR exists on disk afterward.
        _, remember_dir, _ = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )
        remember_root = Path(remember_dir)
        assert remember_root.exists()

        secret = tmp_path / "outside-store-secret.json"
        secret.write_text(json.dumps({"haiku": {"oauth_token": "leaked-if-followed"}}))
        os.symlink(secret, remember_root / "config.json")

        merged, _, stderr = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )

        assert (remember_root / "config.json").is_symlink(), "the symlink was replaced"
        assert "leaked-if-followed" not in json.dumps(merged), (
            "the merged config picked up the symlinked file's content -- "
            "it was read through the link"
        )
        assert "symlink" in stderr and "757" in stderr


class TestSymlinkedLegacyDirectoryNeverMigrated:
    """Scope addition found by the parallel injection-guard lane: the
    legacy `.remember` DIRECTORY itself can be a symlink a repository
    committed, pointing anywhere on disk. The whole-directory `mv` used to
    move the link verbatim into REMEMBER_DIR -- from then on it looks like
    a legitimate operator-configured external directory, and nothing at
    read time can tell the difference. Refused at migration time instead."""

    def test_a_symlinked_legacy_directory_is_not_migrated(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        outside = tmp_path / "outside-data-dir"
        outside.mkdir()
        (outside / "now.md").write_text("outside data\n")
        os.symlink(outside, project / ".remember")

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        _merged, remember_dir, stderr = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )

        legacy = project / ".remember"
        assert legacy.is_symlink(), "the symlinked legacy directory was replaced or removed"
        assert _symlink_targets(legacy, outside), "the symlink does not resolve to the expected target"
        assert not Path(remember_dir).exists() or not (Path(remember_dir) / "now.md").exists(), (
            "the external store ended up holding the symlink target's data -- "
            "the symlinked directory was migrated"
        )
        assert "symlink" in stderr and "757" in stderr

    def test_an_ordinary_legacy_directory_still_migrates(self, tmp_path):
        """Positive control: an ORDINARY (non-symlink) legacy directory must
        still migrate normally."""
        project = tmp_path / "proj"
        project.mkdir()
        pipeline = tmp_path / "plugin"
        pipeline.mkdir()
        home = tmp_path / "home"
        (home / ".remember").mkdir(parents=True)

        _make_legacy_dir(project)

        ext_base = tmp_path / "ext"
        (pipeline / "config.json").write_text(
            json.dumps({"data_dir": f"{_bash_path(ext_base)}/{{slug}}"})
        )

        _, remember_dir, _ = _run_bootstrap_and_dump_merged_config(
            str(project), str(pipeline), str(home)
        )
        legacy = project / ".remember"
        assert not legacy.is_symlink()
        assert not (legacy / "now.md").exists()
        assert (Path(remember_dir) / "now.md").exists()
