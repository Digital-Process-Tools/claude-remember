"""sniff_file_envelope_status() decided the whole file's envelope from its
own FIRST parseable line (#443's own design) -- but current Claude Code
transcripts (2.1.257/2.1.258, CLI and Desktop) no longer open with a
message line at all. They open with several bookkeeping records --
``bridge-session``, ``queue-operation``, ``mode``, ``permission-mode``,
``last-prompt``, ``custom-title``, ``attachment``,
``file-history-snapshot`` -- none of which carries a ``message`` object or
a known ``type``, so ``sniff_envelope()`` returns "unrecognised" for that
first line and the old code returned immediately, treating the whole
transcript as unrecognised and reading zero exchanges (#543).

The fix is to keep scanning past a line ``sniff_envelope()`` cannot place
and return the first NON-"unrecognised" verdict found, falling back to
"unrecognised" only once the file (or the scan cap) is exhausted. A line
neither function can place is not evidence *for* either host -- it is
simply skipped, never treated as a verdict -- so this does not weaken the
"one host wrote the whole file" reasoning ``sniff_envelope()``'s own
docstring gives.

Every "must fire" case here (a real Claude Code transcript, bookkeeping
lines first) is paired with the "must not fire" control this issue says
the existing suite already lacked visible clarity on: a genuinely foreign
file, with no line any recognised shape at all, must still return
"unrecognised" as it always did.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.extract import sniff_file_envelope, sniff_file_envelope_status


def test_bookkeeping_lines_before_the_first_message_are_skipped(tmp_path):
    """The defect itself, reproduced with the issue's own line shapes: a
    current Claude Code transcript that opens with bridge/queue/attachment
    bookkeeping lines -- none placeable by sniff_envelope() -- followed by
    a real ``user`` line carrying a ``message`` object. This must be
    recognised as claude-code, not "unrecognised".
    """
    path = tmp_path / "current-claude-code.jsonl"
    path.write_text(
        '{"type": "bridge-session", "bridgeSessionId": "x", "sessionId": "s"}\n'
        '{"type": "queue-operation", "operation": "enqueue", "sessionId": "s"}\n'
        '{"type": "queue-operation", "operation": "dequeue", "sessionId": "s"}\n'
        '{"type": "attachment", "attachment": {}, "sessionId": "s"}\n'
        '{"type": "user", "message": {"role": "user", "content": "hi"}, "sessionId": "s"}\n',
        encoding="utf-8",
    )
    envelope, unreadable, capped = sniff_file_envelope_status(str(path))
    assert envelope == "claude-code"
    assert unreadable is False
    assert capped is False
    assert sniff_file_envelope(str(path)) == "claude-code"


def test_bookkeeping_lines_before_a_codex_payload_are_skipped(tmp_path):
    """Same shape, the other host: unplaceable lines first, then a line
    whose ``payload`` dict is Codex's own structural marker. Scanning
    forward must not favour claude-code over codex -- it returns whichever
    verdict the first PLACEABLE line actually gives.
    """
    path = tmp_path / "current-codex.jsonl"
    path.write_text(
        '{"type": "mode", "mode": "auto"}\n'
        '{"type": "session_meta", "payload": {"id": "abc"}}\n',
        encoding="utf-8",
    )
    envelope, unreadable, capped = sniff_file_envelope_status(str(path))
    assert envelope == "codex"
    assert unreadable is False
    assert capped is False


def test_scan_cap_gives_up_before_a_resolving_line_past_it(tmp_path):
    """The scan is capped (``_ENVELOPE_SNIFF_SCAN_CAP``, 50) rather than
    unbounded, so a genuinely foreign run of unplaceable lines still fails
    fast instead of reading a huge file to its end. This must stay
    "unrecognised" even though a resolving ``claude-code`` line DOES exist
    in the file, one line past the cap -- proving the cap is enforced
    (never scanned past 50) rather than merely present as an unused
    constant.
    """
    from pipeline.extract import _ENVELOPE_SNIFF_SCAN_CAP

    unplaceable = '{"type": "queue-operation", "operation": "enqueue"}\n' * (_ENVELOPE_SNIFF_SCAN_CAP + 1)
    resolving = '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
    path = tmp_path / "past-the-cap.jsonl"
    path.write_text(unplaceable + resolving, encoding="utf-8")

    envelope, unreadable, capped = sniff_file_envelope_status(str(path))
    assert envelope == "unrecognised"
    assert unreadable is False
    # #556: this scan-cap case is now distinguishable from genuine
    # exhaustion -- it must report capped, not merely "unrecognised".
    assert capped is True


def test_blank_and_malformed_lines_do_not_count_against_the_scan_cap(tmp_path):
    """The scan cap counts unplaceable-but-PARSEABLE lines, not every line
    in the file: a blank line or one that fails ``json.loads`` must not
    spend the budget the cap allots to genuine bookkeeping lines, or a
    transcript with stray blank lines ahead of its real content could be
    given up on before ever reaching a placeable line well within the cap.
    """
    from pipeline.extract import _ENVELOPE_SNIFF_SCAN_CAP

    noise = ("\n" + "not json at all\n") * _ENVELOPE_SNIFF_SCAN_CAP
    resolving = '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
    path = tmp_path / "noisy-but-within-cap.jsonl"
    path.write_text(noise + resolving, encoding="utf-8")

    envelope, unreadable, capped = sniff_file_envelope_status(str(path))
    assert envelope == "claude-code"
    assert unreadable is False
    assert capped is False


def test_a_genuinely_foreign_file_still_reads_unrecognised(tmp_path):
    """MUST-FIRE control's pair: a file where NO line, however far the scan
    goes, is ever placeable by sniff_envelope() must still return
    "unrecognised" -- scanning forward must never manufacture a verdict out
    of lines that are evidence for neither host.
    """
    path = tmp_path / "foreign.jsonl"
    path.write_text(
        '{"type": "bridge-session", "bridgeSessionId": "x"}\n'
        '{"type": "queue-operation", "operation": "enqueue"}\n'
        '{"kind": "totally-unrelated-tool", "data": [1, 2, 3]}\n',
        encoding="utf-8",
    )
    envelope, unreadable, capped = sniff_file_envelope_status(str(path))
    assert envelope == "unrecognised"
    assert unreadable is False
    assert capped is False
    assert sniff_file_envelope(str(path)) == "unrecognised"
