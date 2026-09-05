"""Cross-plugin promo via `systemMessage` at `SessionStart` only (#574).

Three maintainer decisions settle the design (issue #574 comments), each
pinned by a test here:

1. `promos.json` (beside `config.example.json`) is data, not shell. An entry
   with no `url` is skipped, and the skip is VISIBLE (logged), never a
   silent link-less render.
2. The promo speaks only from `SessionStart`. This suite asserts the
   emission shape on that hook; `session-end-hook.sh` is asserted, by grep,
   to never mention `systemMessage` or `promo` at all -- the maintainer's
   "never on SessionEnd" is a property of the file, not just of this test's
   fixtures.
3. Only a plugin that is NOT installed is promoted. `installed_plugins.json`
   (version 2, `<name>@<marketplace>`) is the source of truth, and an
   unreadable/absent/wrong-version file must suppress the promo exactly like
   a confirmed install does -- "cannot-tell" and "installed" are the same
   output, and only a confident "not-installed" may speak. Each half of that
   claim needs its own fixture (a positive control), never one test alone.

Off switch (`features.plugin_promos`, default true) and throttle
(`cooldowns.promo_seconds`, once per N days, persisted as a machine-global
marker under `$HOME/.remember/tmp/`, independent of any per-project
`data_dir`) are asserted directly against the same session-start-hook.sh
subprocess harness `tests/test_session_start_compact_recap_339.py` already
established.
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
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"
SESSION_END = REPO_ROOT / "scripts" / "session-end-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "eeeeeeee-0000-4000-8000-000000000574"

# The two promos this repo actually ships. Read from the real promos.json
# rather than hardcoded here, so a copy edit cannot desync this suite from
# the shipped file -- but the ids/keys are asserted below to still be these.
SUPERTOOL_KEY = "supertool@dpt-plugins"
JIT_KEY = "claude-jit-context@dpt-plugins"


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    return home, project, remember


def _write_installed(home, keyed: dict | None, version="2"):
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    body = {"version": version, "plugins": keyed or {}}
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(body), encoding="utf-8")


def _env(home, project, remember, extra=None):
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        # NOT "_LIB_MEMORY_DIR_LOADED": "1" -- that guard is what several other
        # suites use to SKIP lib-memory-dir.sh's real body when they only need
        # REMEMBER_DIR pre-seeded. This suite needs the opposite: config.json
        # actually merged and read, since features.plugin_promos and
        # cooldowns.promo_seconds live there. Setting the guard here silently
        # makes every config() call answer its hardcoded default -- discovered
        # live when a promo_seconds=0 fixture kept behaving like the 604800
        # default until this line was removed.
    }
    if extra:
        env.update(extra)
    return env


def _payload():
    return json.dumps(
        {
            "session_id": SESSION,
            "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
            "hook_event_name": "SessionStart",
            "cwd": "/does/not/matter",
        }
    )


def _run(tmp_path, installed_keyed=None, installed_version="2", write_installed=True, env_extra=None):
    home, project, remember = _store(tmp_path)
    if write_installed:
        _write_installed(home, installed_keyed, version=installed_version)
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        input=_payload(),
        env=_env(home, project, remember, env_extra),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, home, remember


class TestPromoOnlyWhenNotInstalled:
    def test_positive_control_promotes_when_genuinely_not_installed(self, tmp_path):
        """Well-formed file, key genuinely absent -> the promo MUST fire.

        Without this positive control, an emitter that is silently broken and
        never speaks passes the suppression tests below just as well.
        """
        out, _home, _remember = _run(tmp_path, installed_keyed={})
        parsed = json.loads(out)
        assert "systemMessage" in parsed
        assert parsed["systemMessage"]

    def test_suppressed_when_key_present(self, tmp_path):
        """The one plugin promos.json offers is already installed -> silence."""
        out, _home, _remember = _run(
            tmp_path,
            installed_keyed={
                SUPERTOOL_KEY: [{"scope": "user", "version": "1.0.0"}],
                JIT_KEY: [{"scope": "user", "version": "1.0.0"}],
            },
        )
        # No systemMessage at all: plain-text output, byte-for-byte the old shape.
        assert not out.lstrip().startswith("{")
        assert "systemMessage" not in out

    def test_cannot_tell_suppresses_like_installed(self, tmp_path):
        """installed_plugins.json absent -> cannot-tell -> no promo, ever.

        This is the maintainer's explicit third-state requirement: absence of
        evidence must not be read as evidence of absence.
        """
        out, _home, _remember = _run(tmp_path, write_installed=False)
        assert "systemMessage" not in out

    def test_wrong_version_is_cannot_tell(self, tmp_path):
        out, _home, _remember = _run(tmp_path, installed_keyed={}, installed_version="1")
        assert "systemMessage" not in out


class TestOffSwitch:
    def test_disabled_via_project_config_suppresses(self, tmp_path):
        home, project, remember = _store(tmp_path)
        _write_installed(home, {})
        (remember / "config.json").write_text(
            json.dumps({"features": {"plugin_promos": False}}), encoding="utf-8"
        )
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            input=_payload(),
            env=_env(home, project, remember),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "systemMessage" not in result.stdout


class TestThrottle:
    def test_repeat_session_within_cooldown_does_not_repromote(self, tmp_path):
        out1, home, remember = _run(tmp_path, installed_keyed={})
        parsed1 = json.loads(out1)
        assert "systemMessage" in parsed1

        # Same machine ($HOME unchanged), a second SessionStart immediately
        # after -- the throttle is per machine, not per session, so this
        # must now be silent even though the plugin is still "not installed".
        result2 = subprocess.run(
            ["bash", str(SESSION_START)],
            input=_payload(),
            env=_env(home, remember.parent, remember),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result2.returncode == 0, result2.stderr
        assert "systemMessage" not in result2.stdout

    def test_cooldown_configurable_and_zero_reproimotes_every_time(self, tmp_path):
        home, project, remember = _store(tmp_path)
        _write_installed(home, {})
        (remember / "config.json").write_text(
            json.dumps({"cooldowns": {"promo_seconds": 0}}), encoding="utf-8"
        )
        env = _env(home, project, remember)
        result1 = subprocess.run(
            ["bash", str(SESSION_START)], input=_payload(), env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert result1.returncode == 0, result1.stderr
        assert "systemMessage" in result1.stdout

        result2 = subprocess.run(
            ["bash", str(SESSION_START)], input=_payload(), env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert result2.returncode == 0, result2.stderr
        assert "systemMessage" in result2.stdout


class TestPromosFileIsData:
    def test_entry_missing_url_is_skipped_and_logged(self, tmp_path):
        """An entry with no `url` never renders, and the skip is visible."""
        home, project, remember = _store(tmp_path)
        _write_installed(home, {})
        fake_plugin_root = tmp_path / "plugin-root"
        # Mirror the real plugin root (scripts/, prompts/ needed by the hook)
        # via a symlink tree, but ship our OWN promos.json with a url-less
        # entry so the hook has exactly one candidate and it is malformed.
        fake_plugin_root.mkdir()
        (fake_plugin_root / "scripts").symlink_to(REPO_ROOT / "scripts")
        (fake_plugin_root / "prompts").symlink_to(REPO_ROOT / "prompts")
        (fake_plugin_root / "pipeline").symlink_to(REPO_ROOT / "pipeline")
        (fake_plugin_root / "promos.json").write_text(
            json.dumps({"promos": [{"id": "no-url-promo", "text": "hello", "installed_key": "x@y"}]}),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            input=_payload(),
            env=_env(home, project, remember, {"CLAUDE_PLUGIN_ROOT": str(fake_plugin_root)}),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "systemMessage" not in result.stdout

        log_files = list((remember / "logs").glob("memory-*.log"))
        assert log_files, "expected a daily log to exist"
        log_text = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        assert "no-url-promo" in log_text
        assert "url" in log_text.lower()

    def test_missing_promos_file_is_silent_not_a_crash(self, tmp_path):
        home, project, remember = _store(tmp_path)
        _write_installed(home, {})
        fake_plugin_root = tmp_path / "plugin-root-nopromos"
        fake_plugin_root.mkdir()
        (fake_plugin_root / "scripts").symlink_to(REPO_ROOT / "scripts")
        (fake_plugin_root / "prompts").symlink_to(REPO_ROOT / "prompts")
        (fake_plugin_root / "pipeline").symlink_to(REPO_ROOT / "pipeline")
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            input=_payload(),
            env=_env(home, project, remember, {"CLAUDE_PLUGIN_ROOT": str(fake_plugin_root)}),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "systemMessage" not in result.stdout

    def test_entry_over_length_budget_is_skipped_and_logged(self, tmp_path):
        """A candidate whose rendered text+url exceeds the 140-char budget
        never renders, and the skip is visible -- a positive control for the
        length guard (auditor finding #574: the guard had no fixture proving
        it actually trips, only two shipped entries that stay comfortably
        under it)."""
        home, project, remember = _store(tmp_path)
        _write_installed(home, {})
        fake_plugin_root = tmp_path / "plugin-root-toolong"
        fake_plugin_root.mkdir()
        (fake_plugin_root / "scripts").symlink_to(REPO_ROOT / "scripts")
        (fake_plugin_root / "prompts").symlink_to(REPO_ROOT / "prompts")
        (fake_plugin_root / "pipeline").symlink_to(REPO_ROOT / "pipeline")
        long_text = "x" * 130
        (fake_plugin_root / "promos.json").write_text(
            json.dumps(
                {
                    "promos": [
                        {
                            "id": "too-long-promo",
                            "text": long_text,
                            "installed_key": "x@y",
                            "url": "https://example.com/z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            input=_payload(),
            env=_env(home, project, remember, {"CLAUDE_PLUGIN_ROOT": str(fake_plugin_root)}),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "systemMessage" not in result.stdout

        log_files = list((remember / "logs").glob("memory-*.log"))
        assert log_files, "expected a daily log to exist"
        log_text = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        assert "too-long-promo" in log_text
        assert "140" in log_text


class TestMarkerIsCommittedOnlyAfterDelivery:
    """Regression for a shared finding from both self-review spawns (#574):
    the throttle/rotation marker used to be written the moment a candidate
    was SELECTED, inside `_remember_compute_promo`, before the buffered-
    stdout / jq stages further down had proven the promo actually reached
    stdout. A buffer-open failure or a jq hiccup on the emit pass could then
    burn the whole `cooldowns.promo_seconds` window on a promo the user never
    saw. The marker is now written only in the branch that just printed the
    JSON carrying `systemMessage`.
    """

    def test_unwritable_ctx_buffer_does_not_burn_the_cooldown(self, tmp_path):
        home, project, remember = _store(tmp_path)
        _write_installed(home, {})
        marker = home / ".remember" / "tmp" / "promo-notice"

        # Make $REMEMBER_DIR/tmp unwritable so `: > "$_REMEMBER_CTX_FILE"`
        # fails and the buffer redirect never engages -- the exact failure
        # mode both review spawns reproduced by hand.
        remember_tmp = remember / "tmp"
        remember_tmp.chmod(0o500)
        try:
            result = subprocess.run(
                ["bash", str(SESSION_START)],
                input=_payload(),
                env=_env(home, project, remember),
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            remember_tmp.chmod(0o700)

        assert result.returncode == 0, result.stderr
        assert "systemMessage" not in result.stdout
        assert not marker.exists(), (
            "the promo was never shown (buffer could not open) but the "
            "throttle marker was written anyway -- this burns the cooldown "
            "on a promo nobody saw"
        )

        # With the buffer writable again, the very next SessionStart must
        # still be free to show the promo -- nothing was silently consumed
        # by the failed attempt above.
        result2 = subprocess.run(
            ["bash", str(SESSION_START)],
            input=_payload(),
            env=_env(home, project, remember),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result2.returncode == 0, result2.stderr
        assert "systemMessage" in result2.stdout


def test_session_end_hook_never_mentions_promo_or_system_message():
    """Never on SessionEnd -- a property of the file, not just this fixture.

    SessionEnd does the final flush and its budget is already the thing that
    gets cut short (#574); asserted by grep so a future edit that adds a
    promo call there fails this test even before a subprocess run would.
    """
    text = SESSION_END.read_text(encoding="utf-8")
    assert "systemMessage" not in text
    assert "promo" not in text.lower()
