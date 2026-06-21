"""Tests for prompt template loading and substitution."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import prompts
from pipeline import shell


def _make_template(tmpdir: str, name: str, content: str) -> None:
    os.makedirs(tmpdir, exist_ok=True)
    with open(os.path.join(tmpdir, name), "w") as f:
        f.write(content)


def test_build_save_prompt_substitution(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        _make_template(d, "save-session.prompt.txt",
            "Time: {{TIME}}\nBranch: {{BRANCH}}\nLast: {{LAST_ENTRY}}\n{{EXTRACT}}")
        monkeypatch.setattr(prompts, "PROMPTS_DIR", d)

        result = prompts.build_save_prompt(
            time="10:30",
            branch="master",
            last_entry="did stuff",
            extract="[HUMAN] hello\n[AGENT] hi",
        )
        assert "Time: 10:30" in result
        assert "Branch: master" in result
        assert "Last: did stuff" in result
        assert "[HUMAN] hello" in result


def test_build_ndc_prompt_substitution(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        _make_template(d, "compress-ndc.prompt.txt",
            "Compress:\n{{NOW_CONTENT}}")
        monkeypatch.setattr(prompts, "PROMPTS_DIR", d)

        result = prompts.build_ndc_prompt("## 10:30 | did stuff\ndetails here")
        assert "## 10:30 | did stuff" in result
        assert "details here" in result


def test_build_consolidation_prompt(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        _make_template(d, "consolidate-staging.prompt.txt",
            "Staging:{{STAGING_FILES}}\nRecent:{{RECENT}}\nArchive:{{ARCHIVE}}")
        monkeypatch.setattr(prompts, "PROMPTS_DIR", d)

        result = prompts.build_consolidation_prompt(
            staging_contents={"today-2026-03-12.md": "stuff from yesterday"},
            recent="# Recent\nold",
            archive="# Archive\nolder",
        )
        assert "today-2026-03-12.md" in result
        assert "stuff from yesterday" in result
        assert "# Recent" in result
        assert "# Archive" in result


def test_build_save_prompt_with_real_templates():
    """Integration: verify the real template files load and substitute."""
    real_prompts = os.path.join(os.path.dirname(__file__), "..", "prompts")
    if not os.path.isdir(real_prompts):
        return  # skip if running outside repo
    result = prompts.build_save_prompt(
        time="10:00", branch="master", last_entry="test", extract="test"
    )
    assert "{{TIME}}" not in result
    assert "{{BRANCH}}" not in result


def test_read_template_nonexistent_file_raises(monkeypatch):
    """_read_template() raises FileNotFoundError when the template file doesn't exist."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(prompts, "PROMPTS_DIR", d)
        with pytest.raises(FileNotFoundError):
            prompts._read_template("nonexistent.txt")


def test_build_save_prompt_extract_with_placeholder_literals(monkeypatch):
    """Regression: {{TIME}}/{{BRANCH}} in extract content must not break header substitution."""
    with tempfile.TemporaryDirectory() as d:
        _make_template(d, "save-session.prompt.txt",
            "Time: {{TIME}}\nBranch: {{BRANCH}}\nLast: {{LAST_ENTRY}}\n{{EXTRACT}}")
        monkeypatch.setattr(prompts, "PROMPTS_DIR", d)

        result = prompts.build_save_prompt(
            time="14:32",
            branch="infra/memory",
            last_entry="previous entry",
            extract="[HUMAN] The template uses {{TIME}} and {{BRANCH}} placeholders\n[ASSISTANT] Yes",
        )
        # Header placeholders substituted correctly
        assert "Time: 14:32" in result
        assert "Branch: infra/memory" in result
        # Extract content preserved verbatim (placeholders already consumed)
        assert "{{TIME}} and {{BRANCH}} placeholders" in result


def test_build_consolidation_prompt_empty_staging(monkeypatch):
    """build_consolidation_prompt() with empty staging_contents replaces {{STAGING_FILES}} with ''."""
    with tempfile.TemporaryDirectory() as d:
        _make_template(d, "consolidate-staging.prompt.txt",
            "Staging:{{STAGING_FILES}}\nRecent:{{RECENT}}\nArchive:{{ARCHIVE}}")
        monkeypatch.setattr(prompts, "PROMPTS_DIR", d)

        result = prompts.build_consolidation_prompt(
            staging_contents={},
            recent="# Recent\nnothing new",
            archive="# Archive\nold stuff",
        )
        assert "{{STAGING_FILES}}" not in result
        assert "Staging:\n" in result
        assert "# Recent" in result
        assert "# Archive" in result


def _run_build_prompt(monkeypatch, extract, max_extract_bytes):
    """Drive shell.cmd_build_prompt with a stub template and return the prompt."""
    with tempfile.TemporaryDirectory() as d:
        _make_template(d, "save-session.prompt.txt", "{{EXTRACT}}")
        monkeypatch.setattr(prompts, "PROMPTS_DIR", d)

        extract_file = os.path.join(d, "extract.txt")
        last_entry_file = os.path.join(d, "last.txt")
        output_file = os.path.join(d, "prompt.txt")
        with open(extract_file, "w", encoding="utf-8") as f:
            f.write(extract)
        with open(last_entry_file, "w", encoding="utf-8") as f:
            f.write("(no previous entry)")

        shell.cmd_build_prompt(
            extract_file=extract_file,
            last_entry_file=last_entry_file,
            time="10:30",
            branch="master",
            output_file=output_file,
            max_extract_bytes=max_extract_bytes,
        )
        with open(output_file, encoding="utf-8") as f:
            return f.read()


def test_build_prompt_caps_oversized_extract(monkeypatch):
    """An extract larger than the cap is truncated to its tail with a NOTE."""
    extract = "HEAD_MARKER\n" + ("x" * 5000) + "\nTAIL_MARKER"
    result = _run_build_prompt(monkeypatch, extract, max_extract_bytes=200)

    assert "TAIL_MARKER" in result          # most-recent work survives
    assert "HEAD_MARKER" not in result       # oldest content dropped
    assert "truncated to the last 200" in result
    # Body (note + kept tail) stays within cap + a small note allowance.
    assert len(result.encode("utf-8")) < 200 + 200


def test_build_prompt_keeps_small_extract_intact(monkeypatch):
    """An extract under the cap is passed through unchanged (no NOTE)."""
    extract = "[HUMAN] hi\n[AGENT] hello"
    result = _run_build_prompt(monkeypatch, extract, max_extract_bytes=300000)

    assert result == extract
    assert "truncated" not in result


def test_build_prompt_cap_disabled_with_zero(monkeypatch):
    """max_extract_bytes=0 disables the cap entirely (back-compat default)."""
    extract = "A" * 10000
    result = _run_build_prompt(monkeypatch, extract, max_extract_bytes=0)

    assert result == extract
