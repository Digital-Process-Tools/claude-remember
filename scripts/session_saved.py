"""Was session X saved? -- the jq-less half of session-start-hook.sh's
`session_was_saved` (#667).

Usage: session_saved.py LAST_SAVE_FILE SESSION_ID
Prints "saved" or "unsaved", always exits 0.

Called by literal path through `_remember_run_python` (#898 round 20 --
this body used to live inline, fed to `python -` as a multi-line
single-quoted here-string whose comments spliced quotes with the
close-emit-reopen idiom; the directory's scanner could not follow the
script past it). Same argv, same output, same semantics.

Mirrors $SAVED_QUERY in session-start-hook.sh exactly:

    def isline: type == "number" and ((isnan or isinfinite) | not) and . == floor;
    if (((.sessions // {})[$id]) | isline)
       or (.session == $id and (.line | isline))
    then "saved" else "unsaved" end
"""

import json
import math
import sys


def isline(v):
    # A JSON number, never a bool (Python's bool is an int subclass), finite
    # (excludes both NaN and +/-Infinity -- 1e400 overflows to Infinity, and
    # floor(Infinity) == Infinity, which would otherwise read as a false
    # "saved"), and equal to its own floor (an integer value).
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(v)
        and v == math.floor(v)
    )


def main():
    try:
        with open(sys.argv[1]) as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 -- an unreadable or non-JSON file is "unsaved", as jq's error is
        print("unsaved")
        return

    sid = sys.argv[2]
    if not isinstance(data, dict):
        print("unsaved")
        return

    sessions = data.get("sessions")
    if sessions is not None and not isinstance(sessions, dict):
        # $SAVED_QUERY's own `(.sessions // {})[$id]` throws a hard jq runtime
        # error the instant `.sessions` is present but not an object (or null)
        # -- jq has no `or`-short-circuit past a raised error, so the WHOLE
        # query aborts right there and the shell side reads empty stdout as
        # "unsaved", never reaching the legacy .session/.line fallback below.
        # Falling through here instead would read a corrupted `sessions`
        # value as "saved" whenever a legacy `session`/`line` pair also
        # happened to validate, diverging from real jq on the same file.
        print("unsaved")
        return

    by_session = isinstance(sessions, dict) and isline(sessions.get(sid))
    legacy = data.get("session") == sid and isline(data.get("line"))
    print("saved" if by_session or legacy else "unsaved")


if __name__ == "__main__":
    main()
