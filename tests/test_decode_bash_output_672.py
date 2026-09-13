"""Unit tests for decode_bash_output() (#672).

decode_bash_output lives in tests/_bash_runner.py next to resolve_bash(),
the helper that locates a real POSIX bash. These tests are pure-bytes and
run on every platform -- no bash required -- so the UTF-16LE decode path
is exercised deterministically even on machines (and CI legs) where the
exact WSL-launcher corruption does not reproduce.
"""

from __future__ import annotations

from ._bash_runner import decode_bash_output


def test_utf16le_interleaved_blob_decodes_to_readable_text():
    """Positive control: a UTF-16LE-framed blob -- the shape the WSL
    launcher or a WindowsApps alias stub writes to a redirected pipe --
    must come back as readable text, not NUL-interleaved garbage."""
    raw = "HOOK_DIR=/some/plugin/root/scripts\n".encode("utf-16-le")
    assert b"\x00" in raw  # the corruption signature is actually present
    assert decode_bash_output(raw) == "HOOK_DIR=/some/plugin/root/scripts\n"


def test_plain_utf8_output_is_returned_unchanged():
    """Negative control: ordinary UTF-8 output (what Git Bash writes) must
    pass through untouched -- the decoder must not mangle what is fine."""
    raw = "HOOK_DIR=/some/plugin/root/scripts\n".encode("utf-8")
    assert b"\x00" not in raw
    assert decode_bash_output(raw) == "HOOK_DIR=/some/plugin/root/scripts\n"


def test_empty_stream_returns_empty_string():
    assert decode_bash_output(b"") == ""


def test_short_utf8_output_is_returned_unchanged():
    """Negative control: a 2-byte UTF-8 blob -- exactly the shape a probe
    printing an exit status or a counter produces -- must come back
    unchanged, not misread as UTF-16LE. (The original NUL gate was true for
    every input shorter than 4 bytes because len(raw) // 4 == 0.)"""
    raw = b"1\n"
    assert decode_bash_output(raw) == "1\n"


def test_undecodable_bytes_fall_back_without_raising():
    """A blob that is neither clean UTF-8 nor a usable UTF-16LE frame must
    still return a str (with replacement characters), never raise, and the
    readable slice must survive in the output."""
    raw = b"\xff\xfe\x00\x00garbage\x80\x81"
    out = decode_bash_output(raw)
    assert isinstance(out, str)
    assert "garbage" in out
