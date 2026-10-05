import json
import math
import sys
def isline(v):
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
    except Exception:
        print("unsaved")
        return
    sid = sys.argv[2]
    if not isinstance(data, dict):
        print("unsaved")
        return
    sessions = data.get("sessions")
    if sessions is not None and not isinstance(sessions, dict):
        print("unsaved")
        return
    by_session = isinstance(sessions, dict) and isline(sessions.get(sid))
    legacy = data.get("session") == sid and isline(data.get("line"))
    print("saved" if by_session or legacy else "unsaved")
if __name__ == "__main__":
    main()
