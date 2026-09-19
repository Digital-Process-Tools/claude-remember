"""A cloned repository's own `.remember/config.json` must not choose the
summarizer's credential or override ANTHROPIC_API_KEY-stripping policy (#726).

`_configured_oauth_token()` / `_configured_anthropic_key_policy()`
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
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.haiku import _config_candidates, _remember_dir_is_project_local
from tests.test_jq_free_config import _path_without_jq
from tests.test_layered_config import DETECT_SCRIPT, LIB_SCRIPT, _run_lib

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

    def test_project_haiku_anthropic_api_key_policy_does_not_reach_the_merged_config(
        self, tmp_path
    ):
        """The unnamed second instance the recon for #726 flagged: the
        project layer can ALSO set haiku.anthropic_api_key to force a strip
        of the operator's own ANTHROPIC_API_KEY -- must not carry through
        either."""
        project, pipeline, home = _dirs(tmp_path)
        (pipeline / "config.json").write_text(json.dumps({}))
        remember = project / ".remember"
        remember.mkdir()
        (remember / "config.json").write_text(
            json.dumps({"haiku": {"anthropic_api_key": "strip"}})
        )

        merged, _ = _run_lib_and_dump_config(project, pipeline, home)
        assert merged.get("haiku", {}).get("anthropic_api_key") != "strip"

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
