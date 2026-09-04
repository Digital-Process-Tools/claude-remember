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


def _run_config(tmp_path: Path, key: str, default: str, config_json: dict,
                 without_jq: bool = False) -> str:
    """Source detect-tools + lib-memory-dir + log.sh against a per-project
    config override, and return what config() prints for `key`."""
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
    env = {**os.environ}
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
    return result.stdout.strip()


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
