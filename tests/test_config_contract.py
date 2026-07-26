"""Every documented config option must be read, and every read one documented.

This class of bug has now happened three times: `features.*` documented but
unreadable because `//` treated false as null (#159), `debug` documented and
shipped in config.example.json but passed to `config()` nowhere, and
`thresholds.consolidate_max_bytes` read by the code but missing from the README
table (#176). Each one is a promise the config file makes and the code does not
keep, and each was found by a human reading both sides.

So the two sides are compared here instead.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
EXAMPLE = REPO_ROOT / "config.example.json"

# Where a config key can legitimately be read from.
SOURCE_DIRS = ("scripts", "hooks.d", "hooks", "pipeline")

# `config ".some.key" default` / `config '.some.key' default`
_CONFIG_CALL = re.compile(r"""config\s+["']\.([A-Za-z0-9_.]+)["']""")

# Rows of the config table: | `key` | default | description |
_README_ROW = re.compile(r"^\|\s*`([a-z][A-Za-z0-9_.]*)`\s*\|")

# Keys the README table lists that are not `config()` options: data_dir is
# resolved by lib-memory-dir.sh's own reader before REMEMBER_CONFIG exists, and
# haiku.oauth_token is read by pipeline/haiku.py from the merged config in
# Python. Both are genuinely wired — they just do not go through config().
_NOT_VIA_CONFIG = {"data_dir", "haiku.oauth_token"}

# Documented and read, but deliberately NOT shipped in config.example.json:
# a `"timezone": ""` people copy is a landmine — an empty TZ falls back to UTC
# on macOS/BSD and produces next-day filenames west of UTC, which is why an
# earlier fix removed it (test_log_sh.py::test_config_example_json_is_valid
# asserts it stays out). Documented-but-not-shipped is a deliberate state, so
# it is named here rather than allowed to fail the agreement check.
#
# `debug` joins it for the same reason, and this PR is why: wiring the key up
# made the long-dead `"debug": false` in the example file LIVE, so anyone who
# copied it would have silently flipped save-session.sh from verbose to quiet —
# the exact default-preservation this change promises to protect. A key whose
# absence means "each script keeps its own default" should not ship with a
# value at all.
_DELIBERATELY_NOT_SHIPPED = {"timezone", "debug"}


def _source_files() -> list[Path]:
    files = []
    for d in SOURCE_DIRS:
        root = REPO_ROOT / d
        if not root.is_dir():
            continue
        files.extend(p for p in root.rglob("*") if p.suffix in {".sh", ".py"})
    return files


def _keys_read_by_the_code() -> set[str]:
    keys = set()
    for path in _source_files():
        keys |= set(_CONFIG_CALL.findall(path.read_text(encoding="utf-8")))
    return keys


def _keys_documented_in_readme() -> set[str]:
    """Keys from the config table only.

    Identified by the row that documents `data_dir` — the README has several
    tables whose first column is a backticked name (memory files, hooks, env
    vars), and matching all of them made `now.md` look like a config option.
    """
    lines = README.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if _README_ROW.match(line)
         and _README_ROW.match(line).group(1) == "data_dir"),
        None,
    )
    assert start is not None, "config table not found — has the README moved?"

    keys = set()
    for line in lines[start:]:
        m = _README_ROW.match(line)
        if not m:
            if line.startswith("|"):
                continue
            break  # end of the table
        keys.add(m.group(1))
    return keys


def _example_keys() -> set[str]:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    keys = set()

    def walk(obj, prefix=""):
        for k, v in obj.items():
            if k.startswith("_"):
                continue
            name = f"{prefix}{k}"
            if isinstance(v, dict):
                walk(v, name + ".")
            else:
                keys.add(name)

    walk(data)
    return keys


def test_every_documented_option_is_read_somewhere():
    """#159 and #176 both shipped an option that did nothing."""
    documented = _keys_documented_in_readme() - _NOT_VIA_CONFIG
    read = _keys_read_by_the_code()
    dead = sorted(documented - read)
    assert not dead, (
        f"documented in the README but passed to config() nowhere: {dead}. "
        "Either wire the option up or stop promising it."
    )


def test_every_option_in_the_example_config_is_read_somewhere():
    """config.example.json is what people copy — it must not ship dead keys."""
    shipped = _example_keys() - _NOT_VIA_CONFIG
    read = _keys_read_by_the_code()
    dead = sorted(shipped - read)
    assert not dead, (
        f"shipped in config.example.json but read nowhere: {dead}"
    )


def test_every_option_the_code_reads_is_documented():
    """The other direction: #176's consolidate_max_bytes was readable, worked,
    and appeared in no table — discoverable only by reading source."""
    read = _keys_read_by_the_code()
    documented = _keys_documented_in_readme()
    undocumented = sorted(read - documented)
    assert not undocumented, (
        f"read by the code but absent from the README config table: "
        f"{undocumented}"
    )


def test_the_readme_and_the_example_config_agree():
    """A key in one and not the other is how the last three drifted apart."""
    documented = _keys_documented_in_readme()
    shipped = _example_keys()
    only_readme = sorted(documented - shipped - _NOT_VIA_CONFIG - _DELIBERATELY_NOT_SHIPPED)
    only_example = sorted(shipped - documented)
    assert not only_example, (
        f"in config.example.json but not the README table: {only_example}"
    )
    assert not only_readme, (
        f"in the README table but not config.example.json: {only_readme}"
    )


def _debug_enabled(tmp_path, config_value=None, env_value=None, default="1"):
    """Run debug_enabled with a given config + env and report the verdict."""
    project = tmp_path / "proj"
    (project / ".claude" / "remember").mkdir(parents=True, exist_ok=True)
    (project / ".remember" / "logs").mkdir(parents=True, exist_ok=True)
    cfg = {} if config_value is None else {"debug": config_value}
    (project / ".remember" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    script = f"""
    set -e
    export PROJECT_DIR="{project}"
    source "{REPO_ROOT / "scripts" / "log.sh"}"
    if debug_enabled {default}; then echo VERBOSE; else echo QUIET; fi
    """
    env = dict(**os.environ)
    env.pop("REMEMBER_DEBUG", None)
    if env_value is not None:
        env["REMEMBER_DEBUG"] = env_value
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.skipif(sys.platform == "win32", reason="bash subprocess (#79)")
class TestDebugOptionActuallyWorks:
    """Static agreement is not enough — #176 was a key that was documented,
    shipped, and inert. These drive the switch."""

    def test_config_true_turns_verbosity_on(self, tmp_path):
        assert _debug_enabled(tmp_path, config_value=True, default="0") == "VERBOSE"

    def test_config_false_turns_verbosity_off(self, tmp_path):
        assert _debug_enabled(tmp_path, config_value=False, default="1") == "QUIET"

    def test_env_var_beats_the_config_option(self, tmp_path):
        assert _debug_enabled(tmp_path, config_value=True, env_value="0") == "QUIET"
        assert _debug_enabled(tmp_path, config_value=False, env_value="1") == "VERBOSE"

    def test_unset_keeps_each_callers_own_default(self, tmp_path):
        """save-session.sh passes 1, the git-backup hook passes 0. Collapsing
        those into one shared default would have silently changed the log
        volume of every existing install in one direction or the other."""
        assert _debug_enabled(tmp_path, default="1") == "VERBOSE"
        assert _debug_enabled(tmp_path, default="0") == "QUIET"


def test_the_example_config_ships_no_debug_value():
    """Wiring `debug` up turned a dead key in the example file into a live one.

    `"debug": false` sat there doing nothing for as long as the option was
    unread. The moment it worked, copying the example silenced save-session.sh
    for every such install — a default change nobody asked for, delivered by a
    file people are told to copy.
    """
    assert "debug" not in _example_keys(), (
        "config.example.json ships a `debug` value again — its absence is what "
        "keeps each script's own default"
    )


@pytest.mark.parametrize("key", ["debug", "thresholds.consolidate_max_bytes"])
def test_the_two_keys_that_prompted_this(key):
    """Named explicitly so a regression names itself rather than appearing as
    one entry in a list."""
    assert key in _keys_documented_in_readme(), f"{key} left the README table"
    if key != "debug":
        assert key in _keys_read_by_the_code(), f"{key} is no longer read"
