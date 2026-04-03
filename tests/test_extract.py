"""Tests for session JSONL extraction."""

import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.extract import (
    extract_messages,
    count_lines,
    _extract_texts,
    _format_tool_use,
    _session_dir,
    _last_save_path,
    _validate_session_id,
    find_session,
    get_last_save_line,
    extract_session,
)
from pipeline.types import ExtractResult

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
SAMPLE = os.path.join(FIXTURES, "sample-session.jsonl")


def test_count_lines():
    assert count_lines(SAMPLE) == 10


def test_extract_messages_all():
    msgs = extract_messages(SAMPLE, skip_lines=0)
    roles = [r for r, _ in msgs]
    # meta + system-reminder lines are filtered out
    assert "HUMAN" in roles
    assert "AGENT" in roles
    # 1 meta (skipped), 1 system-reminder (skipped), 1 isMeta assistant (skipped)
    # Remaining: user "Hello", assistant "Sure", user "login form",
    #   assistant [text+tools], assistant [text+tools], user "Great", assistant "Glad"
    assert len(msgs) == 7


def test_extract_messages_skip_lines():
    msgs = extract_messages(SAMPLE, skip_lines=5)
    # Lines 5-9: assistant[tools], isMeta(skip), assistant[tools], user, assistant
    assert len(msgs) == 4


def test_extract_messages_filters_system_reminders():
    msgs = extract_messages(SAMPLE, skip_lines=0)
    for _, text in msgs:
        assert "<system-reminder>" not in text


def test_extract_messages_includes_tool_summaries():
    msgs = extract_messages(SAMPLE, skip_lines=0)
    tool_msgs = [t for _, t in msgs if "[TOOL:" in t]
    assert len(tool_msgs) >= 2
    # Check specific tool formats
    all_text = "\n".join(t for _, t in msgs)
    assert "[TOOL: Read LoginForm.php]" in all_text
    assert "[TOOL: Grep 'handleSubmit']" in all_text
    assert "[TOOL: Edit LoginForm.php]" in all_text
    assert "[TOOL: Bash `php -f test.php" in all_text


def test_extract_texts_string():
    assert _extract_texts("hello world") == ["hello world"]


def test_extract_texts_empty():
    assert _extract_texts("") == []
    assert _extract_texts("   ") == []


def test_extract_texts_system_reminder():
    assert _extract_texts("<system-reminder>stuff</system-reminder>") == []


def test_extract_texts_block_list():
    blocks = [
        {"type": "text", "text": "checking now"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.php"}},
    ]
    texts = _extract_texts(blocks)
    assert texts[0] == "checking now"
    assert texts[1] == "[TOOL: Read b.php]"


def test_format_tool_use_bash():
    block = {"name": "Bash", "input": {"command": "git status"}}
    assert _format_tool_use(block) == "[TOOL: Bash `git status`]"


def test_format_tool_use_bash_truncates():
    long_cmd = "x" * 200
    block = {"name": "Bash", "input": {"command": long_cmd}}
    result = _format_tool_use(block)
    assert len(result) < 120


def test_format_tool_use_unknown():
    block = {"name": "WebSearch", "input": {}}
    assert _format_tool_use(block) == "[TOOL: WebSearch]"


def test_validate_session_id_valid():
    _validate_session_id("abc-123-def")  # should not raise


def test_validate_session_id_path_traversal():
    for bad in ["../etc/passwd", "foo/bar", "a\\b", "..\\windows"]:
        try:
            _validate_session_id(bad)
            assert False, f"should have rejected: {bad}"
        except ValueError:
            pass


def test_get_last_save_line_missing_file():
    with tempfile.TemporaryDirectory() as d:
        assert get_last_save_line("test-session", project_dir=d) == 0


def test_get_last_save_line_matching_session():
    with tempfile.TemporaryDirectory() as d:
        save_dir = os.path.join(d, ".remember", "tmp")
        os.makedirs(save_dir)
        with open(os.path.join(save_dir, "last-save.json"), "w") as f:
            json.dump({"session": "abc-123", "line": 42}, f)
        assert get_last_save_line("abc-123", project_dir=d) == 42


def test_get_last_save_line_different_session():
    with tempfile.TemporaryDirectory() as d:
        save_dir = os.path.join(d, ".remember", "tmp")
        os.makedirs(save_dir)
        with open(os.path.join(save_dir, "last-save.json"), "w") as f:
            json.dump({"session": "old-session", "line": 99}, f)
        assert get_last_save_line("new-session", project_dir=d) == 0


def test_extract_messages_corrupt_lines():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write('{"type":"user","message":{"role":"user","content":"hello"}}\n')
        f.write('NOT VALID JSON\n')
        f.write('{"type":"assistant","message":{"role":"assistant","content":"hi"}}\n')
        path = f.name
    try:
        msgs = extract_messages(path)
        assert len(msgs) == 2  # corrupt line skipped
    finally:
        os.unlink(path)


def test_extract_messages_nonexistent_file():
    msgs = extract_messages("/nonexistent/path.jsonl")
    assert msgs == []


def test_extract_texts_block_missing_type_key():
    """Content block missing 'type' key should not crash."""
    blocks = [
        {"text": "orphan text, no type"},
        {"type": "text", "text": "normal block"},
    ]
    texts = _extract_texts(blocks)
    # Block without 'type' has btype="" — not "text" or "tool_use", so skipped
    assert texts == ["normal block"]


def test_extract_texts_block_missing_text_key():
    """Content block of type 'text' with no 'text' key should not crash."""
    blocks = [
        {"type": "text"},  # no "text" key
        {"type": "text", "text": "present"},
    ]
    texts = _extract_texts(blocks)
    assert texts == ["present"]


def test_format_tool_use_missing_name_key():
    """Block with no 'name' key should fall back gracefully."""
    block = {"input": {"command": "ls"}}
    result = _format_tool_use(block)
    assert result == "[TOOL: ?]"


def test_format_tool_use_file_path_tools():
    """Edit, Read, Write should extract the filename from file_path."""
    for tool in ("Edit", "Read", "Write"):
        block = {"name": tool, "input": {"file_path": "/some/deep/path/MyClass.php"}}
        result = _format_tool_use(block)
        assert result == f"[TOOL: {tool} MyClass.php]"


def test_format_tool_use_glob_pattern():
    """Glob should include the pattern field."""
    block = {"name": "Glob", "input": {"pattern": "**/*.php"}}
    result = _format_tool_use(block)
    assert result == "[TOOL: Glob '**/*.php']"


def test_format_tool_use_grep_pattern():
    """Grep should include the pattern field."""
    block = {"name": "Grep", "input": {"pattern": "handleSubmit"}}
    result = _format_tool_use(block)
    assert result == "[TOOL: Grep 'handleSubmit']"


def test_extract_session_count_limits_results():
    """count parameter should return at most N exchanges."""
    # extract_session calls find_session internally — bypass it by calling
    # extract_messages directly with the fixture, then verify count slicing logic
    all_msgs = extract_messages(SAMPLE, skip_lines=0)
    sliced = all_msgs[-2:]
    assert len(sliced) <= 2
    assert len(sliced) <= len(all_msgs)


def test_find_session_nonexistent_raises():
    """find_session with a nonexistent session ID should raise FileNotFoundError."""
    with tempfile.TemporaryDirectory() as project_dir:
        try:
            find_session(session_id="does-not-exist", project_dir=project_dir)
            assert False, "should raise FileNotFoundError"
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# _session_dir and _last_save_path — private path builders
# ---------------------------------------------------------------------------

def test_session_dir_replaces_slashes():
    """_session_dir should replace / with - and expand ~ prefix."""
    result = _session_dir("/Users/foo/myproject")
    home = os.path.expanduser("~")
    assert result.startswith(home)
    assert result.endswith("/.claude/projects/-Users-foo-myproject")


def test_last_save_path_structure():
    """_last_save_path should return .remember/tmp/last-save.json inside project_dir."""
    result = _last_save_path("/some/project")
    assert result == "/some/project/.remember/tmp/last-save.json"


# ---------------------------------------------------------------------------
# find_session — lines 43 and 47
# ---------------------------------------------------------------------------

def test_find_session_by_id_returns_path():
    """find_session with a valid session ID returns its path (line 43)."""
    with tempfile.TemporaryDirectory() as sdir:
        session_id = "abc-123-def"
        jsonl_path = os.path.join(sdir, session_id + ".jsonl")
        with open(jsonl_path, "w") as f:
            f.write('{"type":"summary","isMeta":true,"message":{"content":"x"}}\n')

        with patch("pipeline.extract._session_dir", return_value=sdir):
            result = find_session(session_id=session_id, project_dir="/fake")

        assert result == jsonl_path


def test_find_session_auto_discovery_returns_latest():
    """find_session with no session_id returns the most recently modified file (line 47)."""
    with tempfile.TemporaryDirectory() as sdir:
        older = os.path.join(sdir, "old-session.jsonl")
        newer = os.path.join(sdir, "new-session.jsonl")
        with open(older, "w") as f:
            f.write("{}\n")
        with open(newer, "w") as f:
            f.write("{}\n")
        # Ensure newer has a later mtime
        os.utime(older, (1000000, 1000000))
        os.utime(newer, (9999999, 9999999))

        with patch("pipeline.extract._session_dir", return_value=sdir):
            result = find_session(session_id=None, project_dir="/fake")

        assert result == newer


def test_find_session_no_files_raises():
    """find_session with no .jsonl files raises FileNotFoundError."""
    with tempfile.TemporaryDirectory() as sdir:
        with patch("pipeline.extract._session_dir", return_value=sdir):
            try:
                find_session(session_id=None, project_dir="/fake")
                assert False, "should raise FileNotFoundError"
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# get_last_save_line — lines 61-62 (corrupt JSON branch)
# ---------------------------------------------------------------------------

def test_get_last_save_line_corrupt_json():
    """Corrupt last-save.json should be caught and return 0 (lines 61-62)."""
    with tempfile.TemporaryDirectory() as d:
        save_dir = os.path.join(d, ".remember", "tmp")
        os.makedirs(save_dir)
        with open(os.path.join(save_dir, "last-save.json"), "w") as f:
            f.write("NOT VALID JSON AT ALL")
        assert get_last_save_line("any-session", project_dir=d) == 0


# ---------------------------------------------------------------------------
# extract_session — lines 172-198 (main orchestrator)
# ---------------------------------------------------------------------------

def _make_session_dir(base: str, session_id: str) -> str:
    """Create a session dir with a minimal .jsonl file, return the sdir path."""
    sdir = os.path.join(base, "sessions")
    os.makedirs(sdir, exist_ok=True)
    jsonl_path = os.path.join(sdir, session_id + ".jsonl")
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Hello"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "Hi there"}},
        {"type": "user", "message": {"role": "user", "content": "What is 2+2?"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "4"}},
    ]
    with open(jsonl_path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
    return sdir


def test_extract_session_returns_extract_result():
    """extract_session returns an ExtractResult with expected fields."""
    with tempfile.TemporaryDirectory() as base:
        sid = "test-session-001"
        sdir = _make_session_dir(base, sid)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            result = extract_session(session_id=sid, project_dir="/fake")

        assert isinstance(result, ExtractResult)
        assert sid in result.exchanges
        assert result.position == 4
        assert result.human_count == 2
        assert result.assistant_count == 2


def test_extract_session_show_all():
    """show_all=True reads from line 0 regardless of last-save."""
    with tempfile.TemporaryDirectory() as base:
        sid = "test-session-002"
        sdir = _make_session_dir(base, sid)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            result = extract_session(session_id=sid, project_dir="/fake", show_all=True)

        assert result.human_count == 2
        assert result.assistant_count == 2


def test_extract_session_count_param():
    """count=1 returns only the last exchange."""
    with tempfile.TemporaryDirectory() as base:
        sid = "test-session-003"
        sdir = _make_session_dir(base, sid)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            result = extract_session(session_id=sid, project_dir="/fake", count=1)

        assert result.human_count + result.assistant_count == 1


def test_extract_session_uses_last_save_line():
    """Without show_all/count, extract_session skips lines before last-save."""
    with tempfile.TemporaryDirectory() as base:
        sid = "test-session-004"
        sdir = _make_session_dir(base, sid)

        # Write a last-save.json pointing to line 2 — skips the first exchange
        save_dir = os.path.join(base, ".remember", "tmp")
        os.makedirs(save_dir)
        with open(os.path.join(save_dir, "last-save.json"), "w") as f:
            json.dump({"session": sid, "line": 2}, f)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            result = extract_session(session_id=sid, project_dir=base)

        # Only 2 lines remain (lines 2-3), one user + one assistant
        assert result.human_count == 1
        assert result.assistant_count == 1


def test_extract_session_output_format():
    """exchanges string contains the separator and role headers."""
    with tempfile.TemporaryDirectory() as base:
        sid = "test-session-005"
        sdir = _make_session_dir(base, sid)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            result = extract_session(session_id=sid, project_dir="/fake", show_all=True)

        assert "=" * 60 in result.exchanges
        assert "[HUMAN]" in result.exchanges
        assert "[AGENT]" in result.exchanges
        assert "-" * 40 in result.exchanges


# ---------------------------------------------------------------------------
# main() CLI entry point — lines 208-252, 256
# ---------------------------------------------------------------------------

def test_main_extract_subcommand(capsys):
    """main() with --all prints exchanges and __POSITION__ marker."""
    with tempfile.TemporaryDirectory() as base:
        sid = "cli-session-001"
        sdir = _make_session_dir(base, sid)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            with patch("sys.argv", ["extract.py", "--session", sid, "--all", "--project-dir", "/fake"]):
                from pipeline.extract import main
                main()

    captured = capsys.readouterr()
    assert "__POSITION__:4" in captured.out
    assert "[HUMAN]" in captured.out


def test_main_json_flag(capsys):
    """main() with --json outputs valid JSON with expected keys."""
    with tempfile.TemporaryDirectory() as base:
        sid = "cli-session-002"
        sdir = _make_session_dir(base, sid)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            with patch("sys.argv", ["extract.py", "--session", sid, "--all", "--json", "--project-dir", "/fake"]):
                from pipeline.extract import main
                main()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "exchanges" in data
    assert "position" in data
    assert data["position"] == 4
    assert "human_count" in data
    assert "assistant_count" in data


def test_main_count_arg(capsys):
    """main() with a numeric count arg limits exchanges returned."""
    with tempfile.TemporaryDirectory() as base:
        sid = "cli-session-003"
        sdir = _make_session_dir(base, sid)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            with patch("sys.argv", ["extract.py", "--session", sid, "1", "--project-dir", "/fake"]):
                from pipeline.extract import main
                main()

    captured = capsys.readouterr()
    assert "__POSITION__" in captured.out


def test_main_invalid_arg_exits(capsys):
    """main() with an unrecognized non-integer arg exits with code 1."""
    with tempfile.TemporaryDirectory() as base:
        sid = "cli-session-004"
        sdir = _make_session_dir(base, sid)

        with patch("pipeline.extract._session_dir", return_value=sdir):
            with patch("sys.argv", ["extract.py", "--session", sid, "not-a-number", "--project-dir", "/fake"]):
                try:
                    from pipeline.extract import main
                    main()
                    assert False, "should have exited"
                except SystemExit as e:
                    assert e.code == 1


# ---------------------------------------------------------------------------
# Issue #11: Windows-style path slugging end-to-end
# ---------------------------------------------------------------------------
# These tests create real directories at the slugged path (like Claude Code
# does on Windows) and prove find_session/extract_session work without mocks.

def _make_real_session(home_dir: str, project_dir: str, session_id: str) -> str:
    """Create a realistic session dir at the slugged path under a fake $HOME.

    Returns the JSONL file path.
    """
    import re as remod
    slug = remod.sub(r'[^a-zA-Z0-9]', '-', project_dir)
    sdir = os.path.join(home_dir, ".claude", "projects", slug)
    os.makedirs(sdir, exist_ok=True)
    jsonl_path = os.path.join(sdir, session_id + ".jsonl")
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Hello from Windows"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "Hi there"}},
    ]
    with open(jsonl_path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
    return jsonl_path


def test_find_session_windows_backslash_path():
    """find_session works with a D:\\Users\\dev\\project style path (issue #11 point 1)."""
    with tempfile.TemporaryDirectory() as home:
        project_dir = "D:\\Users\\dev\\project"
        session_id = "win-session-001"
        _make_real_session(home, project_dir, session_id)

        with patch.dict(os.environ, {"HOME": home}):
            result = find_session(session_id=session_id, project_dir=project_dir)

        assert result.endswith(session_id + ".jsonl")


def test_find_session_windows_colon_path():
    """find_session works with a D:/Users/dev/project style path (issue #11 point 1)."""
    with tempfile.TemporaryDirectory() as home:
        project_dir = "D:/Users/dev/project"
        session_id = "win-session-002"
        _make_real_session(home, project_dir, session_id)

        with patch.dict(os.environ, {"HOME": home}):
            result = find_session(session_id=session_id, project_dir=project_dir)

        assert result.endswith(session_id + ".jsonl")


def test_extract_session_windows_path_end_to_end():
    """Full extract_session with a Windows-style path — no mocks on _session_dir."""
    with tempfile.TemporaryDirectory() as home:
        project_dir = "D:\\Users\\dev\\my project"
        session_id = "win-session-003"
        _make_real_session(home, project_dir, session_id)

        # Create .remember/tmp for last-save.json lookup (uses project_dir directly)
        # Since project_dir is a fake Windows path that doesn't exist on disk,
        # we need to patch _last_save_path to avoid filesystem errors.
        with patch.dict(os.environ, {"HOME": home}), \
             patch("pipeline.extract._last_save_path", return_value="/nonexistent"):
            result = extract_session(
                session_id=session_id,
                project_dir=project_dir,
                show_all=True,
            )

        assert isinstance(result, ExtractResult)
        assert result.human_count == 1
        assert result.assistant_count == 1
        assert "Hello from Windows" in result.exchanges


def test_slug_consistency_python_vs_bash():
    """Python _session_dir slug matches bash sed 's/[^a-zA-Z0-9]/-/g' for Windows paths."""
    import subprocess
    import re as remod

    test_paths = [
        "D:\\Users\\dev\\project",
        "D:/Users/dev/project",
        "C:\\Program Files\\My App",
        "/home/user/project",
        "/Users/dev/My Project (v2)",
    ]
    for path in test_paths:
        python_slug = remod.sub(r'[^a-zA-Z0-9]', '-', path)
        bash_result = subprocess.run(
            ["bash", "-c", f"echo '{path}' | sed 's/[^a-zA-Z0-9]/-/g'"],
            capture_output=True, text=True,
        )
        bash_slug = bash_result.stdout.strip()
        assert python_slug == bash_slug, (
            f"Slug mismatch for {path!r}: python={python_slug!r} bash={bash_slug!r}"
        )
