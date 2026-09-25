"""``config()`` (scripts/log.sh:294) builds its jq program by splicing $key
straight into the program text, twice:

    jq -r "if $key == null then \"\" else ($key | tostring) end" "$REMEMBER_CONFIG"

Every call site today passes a hardcoded literal, so nothing exploits this in
the shipped code. But the function's contract does not say the argument must
be a literal, and nothing enforces it -- a future caller that reads a key name
out of a variable turns a lookup into jq execution against the user's config.
(#539)

This asserts config() rejects anything that is not a plain dotted path
(``^\\.[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)*$``) and returns the caller's default
instead of evaluating it as jq -- with jq on PATH, where the interpolation
actually lives, and paired with a positive control reading a real key so the
malicious-key case cannot pass merely because config() always prints nothing.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX layout — not portable to Windows runners (#79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT_SCRIPT = REPO_ROOT / "scripts" / "detect-tools.sh"
LIB_SCRIPT = REPO_ROOT / "scripts" / "lib-memory-dir.sh"
LOG_SH = REPO_ROOT / "scripts" / "log.sh"

# A key that is not a literal config path but valid jq: if it is spliced
# unquoted into `if $key == null then "" else ($key | tostring) end`, jq
# evaluates the injected expression and prints "true" -- a value that is
# neither the caller's default nor anything in the config file.
_INJECTED_KEY = '("INJECTED"|length>0)'


def _dirs(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    pipeline = tmp_path / "plugin"
    pipeline.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return project, pipeline, home


def _run_config_raw(tmp_path: Path, key: str, default: str, config_json: dict,
                     without_jq: bool = False, env_extra: "dict | None" = None
                     ) -> subprocess.CompletedProcess:
    """Same sourcing as _run_config, but returns the whole CompletedProcess
    (stdout AND stderr) rather than just the printed value -- needed by the
    locale test below, where the printed value alone cannot tell "the guard
    correctly rejected this key" from "the key was accepted but is simply
    absent from config.json either way" (see that test's own docstring)."""
    project, pipeline, home = _dirs(tmp_path)
    (pipeline / "config.json").write_text(json.dumps({}))
    remember = project / ".remember"
    remember.mkdir()
    (remember / "config.json").write_text(json.dumps(config_json))

    script = f"""
    set -e
    export PROJECT_DIR={project}
    export PIPELINE_DIR={pipeline}
    export HOME={home}
    source {DETECT_SCRIPT}
    source {LIB_SCRIPT}
    source {LOG_SH}
    config '{key}' '{default}'
    """
    env = {**os.environ, **(env_extra or {})}
    if without_jq:
        fake_bin = tmp_path / "no-jq-bin"
        fake_bin.mkdir()
        for d in os.environ.get("PATH", "").split(os.pathsep):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if name == "jq":
                    continue
                target = fake_bin / name
                if target.exists() or target.is_symlink():
                    continue
                try:
                    os.symlink(os.path.join(d, name), target)
                except OSError:
                    pass
        env["PATH"] = str(fake_bin)
    result = subprocess.run(["bash", "-c", script], env=env, check=False,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"config() sourcing failed:\n{result.stderr}"
    return result


def _run_config(tmp_path: Path, key: str, default: str, config_json: dict,
                 without_jq: bool = False, env_extra: "dict | None" = None) -> str:
    """Source detect-tools + lib-memory-dir + log.sh against a per-project
    config override, and return what config() prints for `key`."""
    return _run_config_raw(tmp_path, key, default, config_json,
                            without_jq=without_jq, env_extra=env_extra).stdout.strip()


@pytest.mark.skipif(shutil.which("jq") is None, reason="exercises the jq-based interpolation directly")
class TestConfigKeyInjectionWithJq:

    def test_injected_key_returns_default_not_evaluated(self, tmp_path):
        """A key that is valid jq but not a real config path must not be
        evaluated as jq -- config() must fail closed to the default."""
        out = _run_config(tmp_path, _INJECTED_KEY, "safe-default", {"model": "sonnet"})
        assert out == "safe-default", (
            f"config() evaluated the key as a jq program and returned "
            f"{out!r} instead of the default -- the jq interpolation in "
            f"scripts/log.sh's config() is not guarding its $key argument"
        )

    def test_positive_control_real_key_still_reads(self, tmp_path):
        """Paired positive control: a genuine dotted key must still read its
        real value, so the injected-key case above cannot pass merely
        because config() stopped returning anything at all."""
        out = _run_config(tmp_path, ".model", "haiku", {"model": "sonnet"})
        assert out == "sonnet"


def _utf8_locale_available() -> "str | None":
    """An installed locale that ACTUALLY widens [A-Za-z] to accept "é" under
    this machine's bash -- not just any UTF-8-named locale. Not every UTF-8
    locale reproduces the collation quirk (confirmed empirically: nl_NL.UTF-8
    on this repo's own CI-adjacent macOS box does not widen the range, while
    en_US.UTF-8 does), so picking the first UTF-8 name from `locale -a`
    without checking is not good enough -- it can silently pick a locale that
    never triggers the thing under test, at which point the test passes for
    a reason unrelated to the fix. en_US.UTF-8 is tried first since it is
    the one already confirmed to reproduce this; every other installed
    UTF-8-ish locale is tried after it as a fallback. None found (or none
    installed at all) returns None, and the caller skips loudly rather than
    passing vacuously."""
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                              timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    candidates = [line.strip() for line in out.splitlines()
                  if "utf8" in line.lower() or "utf-8" in line.lower()]
    for preferred in ("en_US.UTF-8", "en_US.utf8"):
        if preferred in candidates:
            candidates.remove(preferred)
            candidates.insert(0, preferred)
    for name in candidates:
        try:
            probe = subprocess.run(
                ["bash", "-c", '[[ "é" =~ ^[A-Za-z]+$ ]]'],
                env={**os.environ, "LC_ALL": name, "LANG": name},
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return name
    return None


class TestConfigGuardIsLocaleIndependent:
    """The guard's own comment claims to accept only [A-Za-z0-9_] -- ASCII.
    But `[A-Za-z]` is a POSIX bracket RANGE, matched by collation order
    rather than byte value once LC_COLLATE (via LANG/LC_ALL) selects a
    UTF-8-collating locale, and scripts/lib-slug.sh already documents
    hitting this exact trap elsewhere in this codebase. Under such a locale,
    plain `[[ "$key" =~ [A-Za-z]+ ]]` also matches accented Latin letters,
    which would let a key like `.café` slip past a guard whose whole job is
    rejecting anything the ASCII allowlist does not name.

    This cannot be asserted through config()'s PRINTED VALUE alone: a key
    like `.café` is never a real entry in any config.json this suite writes
    (config.json keys are always ASCII, and the flattener refuses the whole
    file the moment any key is not), so whether the guard correctly rejects
    `.café` or incorrectly waves it through, the final answer is "safe-
    default" either way -- rejected by the guard in the first case, or
    absent from the config in the second. Confirmed empirically: run this
    same assertion against a scratch copy of scripts/log.sh with the
    guard's `( LC_ALL=C; ... )` subshell removed, and the printed value is
    STILL "safe-default" under en_US.UTF-8 -- a test written against the
    printed value would pass whether or not the fix exists, failing this
    repo's own bar (CLAUDE.md: "would this test still pass if the code did
    nothing?").

    So this asserts the one thing that DOES discriminate: with
    REMEMBER_DEBUG=1, the guard's own rejection line
    ("... is not a plain dotted path ...") on stderr. That line is only
    ever printed from inside the guard's reject branch, so its presence is
    direct evidence the regex actually rejected the key -- not a downstream
    coincidence."""

    def test_accented_key_still_rejected_under_utf8_locale(self, tmp_path):
        locale_name = _utf8_locale_available()
        if locale_name is None:
            pytest.skip(
                "no UTF-8 locale installed on this runner -- the "
                "locale-widening this test guards against cannot be "
                "reproduced here, so this leg tests nothing about it"
            )
        result = _run_config_raw(
            tmp_path, ".café", "safe-default", {"model": "sonnet"},
            env_extra={"LC_ALL": locale_name, "LANG": locale_name,
                       "REMEMBER_DEBUG": "1"})
        assert result.stdout.strip() == "safe-default"
        assert "is not a plain dotted path" in result.stderr, (
            f"config() did not report rejecting '.café' under "
            f"{locale_name!r} -- [A-Za-z] likely widened under this "
            f"locale's collation to match the accented letter and the "
            f"guard let it through silently (the printed value alone "
            f"cannot show this: see this class's docstring), the same "
            f"trap scripts/lib-slug.sh documents and guards against with "
            f"LC_ALL=C"
        )

    def test_positive_control_real_key_still_reads_under_utf8_locale(self, tmp_path):
        locale_name = _utf8_locale_available()
        if locale_name is None:
            pytest.skip("no UTF-8 locale installed on this runner")
        result = _run_config_raw(
            tmp_path, ".model", "haiku", {"model": "sonnet"},
            env_extra={"LC_ALL": locale_name, "LANG": locale_name,
                       "REMEMBER_DEBUG": "1"})
        assert result.stdout.strip() == "sonnet"
        assert "is not a plain dotted path" not in result.stderr, (
            "a genuine, present config key was rejected as malformed -- "
            "the rejection-line assertion above would be meaningless if "
            "the guard fired on ordinary valid keys too"
        )


class TestConfigKeyInjectionWithoutJq:
    """Same two cases through the jq-free fallback path, which reads the key
    by splitting on '.' rather than interpolating it -- already safe from
    this class of injection, but the guard in config() must reject the
    malformed key uniformly regardless of which branch would have handled
    it, so the two paths cannot diverge on which keys they accept."""

    def test_injected_key_returns_default_not_evaluated(self, tmp_path):
        out = _run_config(tmp_path, _INJECTED_KEY, "safe-default",
                           {"model": "sonnet"}, without_jq=True)
        assert out == "safe-default"

    def test_positive_control_real_key_still_reads(self, tmp_path):
        out = _run_config(tmp_path, ".model", "haiku", {"model": "sonnet"},
                           without_jq=True)
        assert out == "sonnet"
