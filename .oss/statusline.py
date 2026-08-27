#!/usr/bin/env python3
# Managed by the oss plugin. This file is OVERWRITTEN every time /oss:scaffold
# runs, so an edit here is lost at the next update. To change what it does,
# copy it somewhere outside .oss/ and point at your copy.

"""One status line for a repository this loop manages (#479).

Claude Code pipes a JSON payload in on stdin once per assistant message and prints
whatever this writes on stdout. That cadence is the whole design constraint: a render
that makes a network call makes one every message, so the forge counts come from a cache
that a detached ``--refresh`` run repopulates, and the render itself only reads files.

**Every field has three states and the third is never rounded up.** A count nobody took
prints `?`, never `0`. A version comparison nobody could make prints `?`, never a tick.
A transcript this process did not read to the bottom cannot say "no tick is armed" -- it
says `?`, because a window that did not reach the top of the file is not a file with
nothing in it. That is the defect class this repository is named after, and a status line
is where it is easiest to commit: the render always produces *something*, so a wrong
answer looks exactly like a right one.

Nothing here is hardcoded about any repository. The forge slug comes from the managed
repo's own ``.oss.json``; the plugin repositories come from each installed plugin's own
manifest, the same derivation ``scripts/doctor.py`` uses and for the same reason.

No third-party imports: this file is vendored into ``.oss/statusline.py`` in repositories
that install nothing to run it.

Python 3.9 compatible.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

#: How much of the transcript tail is scanned for the last ScheduleWakeup. A transcript
#: is append-only and can reach tens of megabytes, and this runs once per message.
DEFAULT_TAIL_BYTES = 2 * 1024 * 1024

#: How old a cached board reading may be before a refresh is forked, in seconds. Short,
#: because this is the half a maintainer watches move: at 300 the line showed a merged pull
#: request and three still-open issues that had just been closed (#515).
REFRESH_AFTER = 60

#: The same, for the version each installed plugin's source repository publishes. Four of a
#: refresh's seven forge calls are these, and they answer a question that changes on the
#: order of weeks -- so they are carried forward between long intervals rather than making
#: the board wait on them.
LATEST_REFRESH_AFTER = 3600

#: How long a refresh may hold its lock before another render is allowed to retry. A
#: lock that outlives a killed refresher would otherwise freeze the counts forever.
LOCK_STALE_AFTER = 180

#: How far back the release-progress field reads the log. Bounded because this runs once
#: per message and a repository's history is not: an unbounded `git rev-list` is fine in
#: this repo at a few hundred commits and is megabytes of output in a large one. A window
#: that does not reach the previous tag reports the missing half rather than a smaller one.
RELEASE_WINDOW = 500

#: How many recent releases the typical size is taken over. A release train that changed
#: pace is described by its recent pace; the whole history would average the change away.
RELEASE_GAPS = 5

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"


# --------------------------------------------------------------------------- values


def parse_timestamp(text):
    """An ISO-8601 stamp from a transcript record, as epoch seconds, or ``None``.

    ``datetime.fromisoformat`` does not accept a trailing ``Z`` before 3.11 and this
    file runs on 3.9, so the suffix is normalised before parsing rather than after.
    """
    if not text:
        return None
    import datetime

    cleaned = str(text).strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return None


def _version_tuple(text):
    if not text:
        return None
    cleaned = str(text).strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    parts = []
    for chunk in cleaned.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if digits == "":
            return None
        parts.append(int(digits))
    return tuple(parts) if parts else None


def version_status(installed, latest, stale=False):
    """Compare an installed version against the latest published one.

    Four states, and two of them are not findings: ``current``, ``behind``, ``ahead``
    (a clone running unreleased work, which is the normal state in this repository's own
    checkout) and ``unknown``. ``unknown`` covers either half of the comparison being
    missing, and it must never render as ``current`` -- nobody asked the forge is not the
    same answer as the forge saying yes.

    ``stale`` marks the comparison itself untrustworthy rather than either side of it
    (#550): a `latest` reading correct when it was taken and false before its own
    refresh interval expires renders identically to a fresh one unless its age
    travels with it to this call. Folded into ``unknown`` -- the same bucket a
    comparison nobody could make already uses -- rather than inventing new
    vocabulary, per the issue's own suggested direction. This does NOT catch a
    reading that is fresh by its own rule and simply wrong, which is what the
    incident this was filed from actually was; that gap belongs to #549, which
    invalidates the cache at the moment a publish falsifies it.
    """
    mine = _version_tuple(installed)
    theirs = _version_tuple(latest)
    if stale or mine is None or theirs is None:
        state = "unknown"
    elif mine == theirs:
        state = "current"
    elif mine < theirs:
        state = "behind"
    else:
        state = "ahead"
    return {"state": state, "installed": installed, "latest": latest}


# ---------------------------------------------------------------------------- board


def board_from_cache(cache, now=None):
    """Read the two forge counts back out of a cache document.

    Each count is read on its own. A cache written by a refresh where one call answered
    and the other did not is a real state, and collapsing it to "unknown board" throws
    away the half that was measured.
    """
    if not isinstance(cache, dict):
        return {"state": "unknown", "prs": None, "issues": None, "age": None}
    prs = cache.get("prs")
    issues = cache.get("issues")
    prs = prs if isinstance(prs, int) else None
    issues = issues if isinstance(issues, int) else None
    checks = cache.get("pr_checks")
    if not (
        isinstance(checks, dict)
        and all(isinstance(checks.get(key), int) for key in ("green", "red", "running", "unknown"))
    ):
        # A cache written before this field existed, or by a refresh whose rollup call did
        # not answer. Neither is "every pull request is green".
        checks = None
    fetched = cache.get("fetched_at")
    age = None
    if isinstance(fetched, (int, float)):
        age = max(0.0, (time.time() if now is None else now) - fetched)
    if prs is None and issues is None:
        state = "unknown"
    elif prs is None or issues is None:
        state = "partial"
    else:
        state = "measured"
    return {"state": state, "prs": prs, "issues": issues, "checks": checks, "age": age}


# ------------------------------------------------------------------ release progress


def release_progress(commits, tags_by_hash):
    """How far into the next release this clone is: commits banked, over the usual size.

    Both halves come from the same two facts -- the log window and where the version tags
    sit in it -- so they are in the same unit and cannot describe different things. And
    both are separately absent: a repository with no version tag has no boundary to count
    from, and one with a single tag has a boundary but no gap to take a size over. Neither
    renders as `0`, which is a measurement this repository takes seriously enough to name
    itself after: zero commits since the tag is a real and common state, and it has to stay
    distinguishable from never having looked.

    `commits` is newest-first, as `git rev-list` prints it. `tags_by_hash` maps a commit to
    the tag names on it; anything `_version_tuple` cannot parse is not a release boundary
    (`wip/274-preserved` is a real tag in this repository and shipped nothing).

    The newest release is chosen by version, not by position in the log: a hotfix tagged
    on an older commit sits further back than a tag it supersedes.
    """
    unknown = {"state": "unknown", "since": None, "typical": None}
    if not commits:
        return unknown
    found = []
    for index, sha in enumerate(commits):
        for tag in tags_by_hash.get(sha) or []:
            version = _version_tuple(tag)
            if version is not None:
                found.append((version, index))
    if not found:
        return unknown
    found.sort(key=lambda pair: pair[0], reverse=True)
    since = found[0][1]
    gaps = []
    for (_, newer), (_, older) in zip(found, found[1:]):
        # A non-positive gap means the log order disagrees with the version order -- two
        # tags on one commit, or a tag cut from a branch. That pair measures nothing, so
        # it is dropped rather than counted as a release of zero commits.
        if older > newer:
            gaps.append(older - newer)
        if len(gaps) == RELEASE_GAPS:
            break
    if not gaps:
        return {"state": "partial", "since": since, "typical": None}
    ordered = sorted(gaps)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        typical = ordered[middle]
    else:
        typical = int(round((ordered[middle - 1] + ordered[middle]) / 2.0))
    return {"state": "measured", "since": since, "typical": typical}


def git_release_progress(root, window=RELEASE_WINDOW):
    """``release_progress`` over this clone's own log. Two git calls, no network.

    Local git rather than the forge on purpose: this field must be right on a render that
    happens once per message, and the cached forge counts beside it are up to
    ``REFRESH_AFTER`` seconds old. A commit that just landed would otherwise not move the
    numerator until that interval expires -- the one moment somebody is looking at it.

    ``for-each-ref`` rather than ``show-ref`` because an annotated tag's own object hash is
    not the commit's: ``*objectname`` dereferences it, and is empty for a lightweight tag,
    so one format string covers both without a second call to tell them apart.
    """
    refs = _run(["git", "-C", str(root), "for-each-ref",
                 "--format=%(objectname) %(*objectname) %(refname:short)", "refs/tags"])
    log = _run(["git", "-C", str(root), "rev-list", "-n", str(window), "HEAD"])
    if refs is None or log is None:
        # Not a git repository, or git could not answer. Nothing was measured, and the
        # field says so rather than reporting a release with no commits in it.
        return {"state": "unknown", "since": None, "typical": None}
    tags = {}
    for line in refs.splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        direct, dereferenced, name = parts
        tags.setdefault(dereferenced or direct, []).append(name)
    return release_progress(log.split(), tags)


#: The rollup states GitHub reports that mean the checks passed, and the ones that mean
#: they have not finished. Everything else -- cancelled, neutral, skipped, timed out, and a
#: pull request carrying no checks at all -- is none of them, and lands in `unknown` rather
#: than being folded into green. A cancelled run is not a pass; reading it as one is how a
#: status line comes to report a board that is fine.
#: A leg that finished and did not pass, and needs somebody. `TIMED_OUT` and
#: `ACTION_REQUIRED` are in here rather than in the group below because a leg that ran out
#: of time is a leg that failed to answer.
ROLLUP_RED = ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE")
ROLLUP_RUNNING = ("PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED")
ROLLUP_GREEN = ("SUCCESS",)


def rollup_state(legs):
    """What CI says about one pull request, from its own legs.

    ``red`` if any leg finished without passing, ``running`` if any leg has not finished,
    ``green`` only if there is at least one leg and every one of them passed. Everything
    else is ``unknown``: a cancelled, skipped, neutral or stale leg is not a pass and not a
    pending -- the rule this repository already applies when reading a pull request's
    checks before a merge -- and a pull request carrying no legs at all has had nothing
    said about it, which is not the same as being fine.

    Computed here rather than read off GitHub's own `statusCheckRollupState`, for two
    reasons and the second is the one that matters. The first is that `gh 2.50.0` does not
    carry that field and answers `Unknown JSON field`, so the whole column read `?` on the
    machine this was written on. The second is that the mapping above is a decision about
    what a maintainer needs to see -- that a cancelled leg is not a pass -- and taking it
    from a precomputed verdict puts it somewhere no test here can reach.
    """
    if not isinstance(legs, list) or not legs:
        return "unknown"
    seen = set()
    for leg in legs:
        if not isinstance(leg, dict):
            seen.add("unknown")
            continue
        status = str(leg.get("status") or "").upper()
        conclusion = str(leg.get("conclusion") or "").upper()
        state = str(leg.get("state") or "").upper()
        if status and status != "COMPLETED":
            seen.add("running")
        elif conclusion in ROLLUP_RED or state in ROLLUP_RED:
            seen.add("red")
        elif conclusion in ROLLUP_GREEN or state in ROLLUP_GREEN:
            seen.add("green")
        elif state in ROLLUP_RUNNING:
            seen.add("running")
        else:
            seen.add("unknown")
    for verdict in ("red", "running", "unknown"):
        if verdict in seen:
            return verdict
    return "green"


def check_rollup_counts(rows, total):
    """Open pull requests grouped by what CI says, or ``None`` if nothing was read.

    ``rows`` is what ``gh pr list --json number,statusCheckRollupState`` returned, and it
    is capped by a page limit while ``total`` comes from an exact count. The difference is
    not zero and it is not green: those are pull requests nobody read, so they land in
    ``unknown`` and the four groups sum to the total. A row count larger than the total --
    a stale count against a fresher page -- clamps at zero rather than going negative.

    ``None`` for a reading that did not happen. A dict of four zeros means the forge was
    asked and answered that there is nothing open, which is a different fact.
    """
    if not isinstance(rows, list):
        return None
    counts = {"green": 0, "red": 0, "running": 0, "unknown": 0}
    for row in rows:
        legs = row.get("statusCheckRollup") if isinstance(row, dict) else None
        counts[rollup_state(legs)] += 1
    if isinstance(total, int):
        counts["unknown"] += max(0, total - len(rows))
    return counts


def cache_dir():
    """Where the cached board lives -- outside the managed repository, always.

    A status line must not write into somebody's tree. `.oss/` is ours and would be a
    candidate, but a cache file is machine state rather than repository content, and it
    would arrive in `git status` on every clone.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "oss-statusline"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "oss-statusline"


def board_is_due(cache, now):
    """Is the board half of the cache older than its own interval (#515)?

    Or has something said so outright: `stale_after` is written by the `PostToolUse` hook
    when this session itself merges a pull request or closes an issue (#516), because the
    interval alone leaves the line wrong for exactly the seconds it is most watched. It is
    a timestamp rather than a flag so that the forge's own search-index lag can be waited
    out -- a refresh taken the instant a merge returns can record the pre-merge counts.
    """
    if isinstance(cache, dict):
        stale_after = cache.get("stale_after")
        if isinstance(stale_after, (int, float)) and now >= stale_after:
            return True
    return _is_due(cache, "fetched_at", REFRESH_AFTER, now)


def mark_board_stale(repo, now=None, delay=0):
    """Say that this repo's cached board is out of date as of ``now + delay`` (#516).

    Rewrites the stamp and nothing else: the counts stay readable until a refresh replaces
    them, because a board that is known-stale is still better than `?` while the refresh
    runs. Silent on any failure -- the caller is a hook on every `Bash` call.
    """
    now = time.time() if now is None else now
    path = cache_path(repo)
    document = read_cache(path)
    document = document if isinstance(document, dict) else {}
    document["stale_after"] = now + delay
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(document), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        return False
    return True


def latest_is_due(cache, now):
    """The same question for the published plugin versions, on the long clock.

    A cache written before this split carries `fetched_at` and no `latest_fetched_at`, and
    that one stamp is when those versions were fetched -- so it is what the age is measured
    from. Reading a missing stamp as "just now" would freeze the version column for a whole
    interval on every upgrade, which is the quiet direction to be wrong in.
    """
    if isinstance(cache, dict) and not isinstance(cache.get("latest_fetched_at"), (int, float)):
        return _is_due(cache, "fetched_at", LATEST_REFRESH_AFTER, now)
    return _is_due(cache, "latest_fetched_at", LATEST_REFRESH_AFTER, now)


def _is_due(cache, key, interval, now):
    if not isinstance(cache, dict):
        return True
    stamp = cache.get(key)
    if not isinstance(stamp, (int, float)):
        return True
    return (now - stamp) > interval


def cache_path(repo):
    slug = "".join(char if char.isalnum() else "-" for char in (repo or "unknown"))
    return cache_dir() / (slug + ".json")


def read_cache(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------ next tick


def _wakeup_input(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("name") == "ScheduleWakeup" and isinstance(block.get("input"), dict):
            return block["input"]
    return None


def _tail_lines(path, max_bytes):
    """The last ``max_bytes`` of a file as whole lines, plus whether anything was cut.

    The truncation flag is the load-bearing return value. Without it a wakeup armed
    above the window is indistinguishable from no wakeup at all, and the status line
    would confidently report an unarmed loop.
    """
    size = os.path.getsize(path)
    truncated = size > max_bytes
    with open(path, "rb") as handle:
        if truncated:
            handle.seek(size - max_bytes)
            handle.readline()  # discard the partial line the seek landed inside
        return handle.read().splitlines(), truncated


def _scan_transcript(transcript_path, max_bytes):
    """The transcript tail, read once, for callers that each need their own answer
    out of it (#504). It had two callers -- ``next_tick`` and the user-age field #513
    removed -- and keeping the split is what makes the error state one thing rather
    than each caller's own guess at why a file could not be read.

    Returns ``(lines, truncated, error)``; on error the first two are ``None`` and
    ``error`` is the detail string a caller's ``unknown`` state should carry.
    """
    if not transcript_path:
        return None, None, "no transcript path in the payload"
    try:
        lines, truncated = _tail_lines(transcript_path, max_bytes)
    except OSError as exc:
        return None, None, "transcript unreadable: {}".format(exc)
    return lines, truncated, None


def next_tick(transcript_path, now=None, max_bytes=DEFAULT_TAIL_BYTES):
    """When the next tick fires, from the last ScheduleWakeup in the transcript.

    Five states: ``armed`` (seconds left), ``due`` (its time has passed and nothing has
    fired yet, which is worth seeing), ``stopped`` (the loop was stopped deliberately),
    ``none`` (the whole file was read and holds no wakeup), and ``unknown`` -- no
    transcript, an unreadable one, or a tail scan that did not reach the top of the file.
    """
    lines, truncated, error = _scan_transcript(transcript_path, max_bytes)
    if error is not None:
        return {"state": "unknown", "detail": error}
    return _next_tick_from_lines(lines, truncated, now=now)


def _next_tick_from_lines(lines, truncated, now=None):
    now = time.time() if now is None else now
    found = None
    for raw in lines:
        if b"ScheduleWakeup" not in raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        payload = _wakeup_input(record)
        if payload is not None:
            found = (record, payload)

    if found is None:
        if truncated:
            return {
                "state": "unknown",
                "detail": "the tail scan did not reach the top of the transcript",
            }
        return {"state": "none", "detail": "no wakeup in this transcript"}

    record, payload = found
    if payload.get("stop"):
        return {"state": "stopped", "detail": "the loop was stopped"}
    delay = payload.get("delaySeconds")
    stamp = parse_timestamp(record.get("timestamp"))
    if not isinstance(delay, (int, float)) or stamp is None:
        return {"state": "unknown", "detail": "the wakeup carried no readable delay"}
    seconds = stamp + delay - now
    state = "armed" if seconds > 0 else "due"
    return {"state": state, "seconds": seconds, "reason": payload.get("reason")}


def _render_stamp(now):
    """The wall-clock reading for the "stamp of the last render" field (#504).

    **The machine's local zone, not UTC (#511).** This shipped as UTC on the reasoning that
    the transcript timestamps ``parse_timestamp`` reads carry no zone either, and that a
    stamp meaning two different clocks depending on where it ran would be worse than one
    that is merely frozen. The first half is about parsing -- ``parse_timestamp`` returns
    epoch seconds, which are unambiguous by the time they arrive here -- and the second
    describes a risk this field does not carry: the stamp is produced and read on one
    machine, in the same second, by the person looking at it.

    What it did carry was the defect the field exists to prevent. ``_last_field`` renders a
    clock time rather than an age precisely so the reader can subtract it from their own
    clock and recover how stale the line is; a UTC stamp makes that subtraction silently
    wrong in every zone but one. Measured at `last 10:11` against a wall clock reading
    12:15.

    ``None`` when the platform cannot convert the instant, so ``_last_field`` renders `?`.
    Falling back to UTC under a label that means local would be this same defect one layer
    down, and quieter.
    """
    try:
        return time.strftime("%H:%M", time.localtime(now))
    except (OSError, OverflowError, ValueError):
        return None


# --------------------------------------------------------------------------- render


def _symbols(ascii_only):
    if ascii_only:
        return {
            "sep": " | ",
            "dot": " . ",
            "current": "",
            "behind": ">",
            "ahead": "+",
            "ok": "ok",
            "bad": "x",
            "run": "...",
            "unk": "?",
        }
    return {
        "sep": " | ",
        "dot": " · ",
        "current": " ✓",
        # Distinct shapes, not just distinct colour (#550): these two markers print
        # different fields -- `behind` names the latest published version, `ahead`
        # names what is installed -- and `⇡`/`↑`, one codepoint apart, were told
        # apart reliably only by colour. Measured: this was the proximate cause of
        # a maintainer reading a correct 0.13.0 install as "not on 0.13.0" (#549).
        # `↥` (arrow from bar) and `↑` differ in silhouette at terminal size even in
        # monochrome. Both still fail to encode under cp1252 exactly as the pair
        # they replace did, so the ASCII fallback below (already unambiguous, `>`
        # vs `+`) is unaffected and this changes nothing about which platforms take
        # that branch.
        "behind": " ↥",
        "ahead": " ↑",
        "ok": "✓",
        "bad": "✗",
        "run": "⋯",
        "unk": "?",
    }


def _duration(seconds):
    seconds = int(abs(seconds))
    if seconds < 90:
        return "{}s".format(seconds)
    minutes = seconds // 60
    if minutes < 90:
        return "{}m".format(minutes)
    return "{}h{:02d}".format(minutes // 60, minutes % 60)


def _tick_field(tick):
    state = (tick or {}).get("state")
    if state == "armed":
        seconds = tick.get("seconds")
        if not isinstance(seconds, (int, float)):
            seconds = 0
        return "tick " + _duration(seconds)
    if state == "due":
        return "tick due"
    if state == "stopped":
        return "tick off"
    if state == "none":
        return "tick -"
    return "tick ?"


def _last_field(stamp):
    """A wall-clock reading of when this line was last rendered, or `?` (#504).

    Freezes between renders like everything else on this line, but a frozen
    clock time stays readable -- the reader compares it against their own
    clock and recovers the staleness, which a frozen age cannot do.

    Folded through `_one_line`: `stamp` normally comes from `_render_stamp`, which
    only ever emits digits and a colon, but `render()`'s own property test (#493)
    treats every string-valued fact as untrusted by construction, so this field
    is folded the same way `repo_name` and `model` are rather than trusted for
    being internally produced.
    """
    return "last " + (_one_line(str(stamp)) if stamp else "?")


def _board_field(board, symbols, color=False):
    """`4pr 2ok 1x 1... 0? . 23is` -- how many are open, and what CI says about each.

    Lowercase because the fields either side of it are, and a status line that shouts one
    field trains the eye to read that one first regardless of what it says.

    **Every group renders, including the ones at zero.** A group that disappears when empty
    makes the reader subtract to find what is missing, and `0x` -- nothing red -- and `0...`
    -- nothing on the way -- are two of the more useful things this line can say. The one
    thing that does collapse is a reading that never happened: rollups nobody could fetch
    render as a single `?`, never as four zeros.
    """
    prs = board.get("prs")
    issues = board.get("issues")
    checks = board.get("checks")
    if isinstance(checks, dict):
        groups = " ".join(
            _group(checks.get(key), symbols[symbol], shade, color)
            for key, symbol, shade in (
                ("green", "ok", GREEN),
                ("red", "bad", RED),
                ("running", "run", YELLOW),
                ("unknown", "unk", DIM),
            )
        )
    else:
        groups = symbols["unk"]
    return "{}pr {}{}{}is".format(
        "?" if not isinstance(prs, int) else prs,
        groups,
        symbols["dot"],
        "?" if not isinstance(issues, int) else issues,
    )


def _group(count, symbol, shade, color):
    """One group. A zero is dimmed rather than coloured: it is news, not an alarm."""
    text = "{}{}".format("?" if not isinstance(count, int) else count, symbol)
    if not color:
        return text
    return (shade if count else DIM) + text + RESET


def _release_field(progress):
    """`rel 4/17` -- banked since the last release, over what a release here usually costs.

    Each half carries its own `?`, because they fail separately: a clone with one tag knows
    exactly how much is banked and nothing about the usual size, and `rel 4/?` says that
    where a single `?` would throw away the half that was measured.
    """
    progress = progress or {}
    since = progress.get("since")
    typical = progress.get("typical")
    return "rel {}/{}".format(
        "?" if not isinstance(since, int) else since,
        "?" if not isinstance(typical, int) else typical,
    )


def _one_line(text, limit=200):
    """Text from outside this script, reduced to one printable ASCII line.

    Adopted verbatim from ``doctor.py``'s function of the same name (itself copied
    from ``release_delta.py``), whose reasoning applies here unchanged: a newline in
    foreign text forges a line of this script's own output, and a control character
    -- an ESC in particular -- can rewrite what the terminal has already printed.
    This status line is one line by construction; nothing that reaches it is
    legitimately multi-line.

    It is a copy rather than an import for the same reason as the original: this is
    a security control on a script meant to run standalone, and it must not depend
    on an import that can fail.

    Applied at the point each value enters -- `version` from this repo's own tracked
    manifest, `installed` and `latest` from a plugin's manifest, the second of which
    is fetched over the network from another repository -- rather than folding the
    whole assembled line, because this script adds its own ANSI colour after this
    point and a line-wide fold would strip those escapes along with a forged one.
    """
    flat = " ".join(str(text).split())
    safe = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in flat)
    return safe[:limit]


def _short_version(text):
    """`v0.11.0` and `0.11.0` are the same version, and a status line has one column.

    A release tag carries the prefix and a manifest does not, so the raw pair renders as
    `0.9.0 -> v0.11.0` -- two spellings of one thing, in the field whose whole job is to
    make a difference obvious.

    Folded through `_one_line` before anything else touches it: this is the one funnel
    both `installed` and `latest` pass through in `_plugin_field`, and `latest` in
    particular is a remote repository's manifest string, fetched over the network.
    """
    if not text:
        return None
    text = _one_line(str(text).strip())
    return text[1:] if text[:1] in ("v", "V") else text


def _short_name(name):
    """A plugin name at status-line width, by rule rather than by table.

    A leading `claude-` says which ecosystem the plugin is in, which is not news on a
    line about this ecosystem, so it goes. What is left is capped at four characters --
    enough to tell the installed set apart, and derived, so a plugin nobody has written
    yet gets a label without anybody adding a row here. A per-name map would be the
    per-repo fact this codebase keeps out of shared code, and it would be wrong the
    first time a plugin is renamed.

    Folded through `_one_line` first: this text is a dependency name declared inside
    another plugin's own tracked manifest (`plugin_facts`'s `record["dependencies"]`),
    the same class of foreign text as `version`/`installed`/`latest` -- and folding
    after the truncation below would be too late, since a newline or ESC surviving a
    four-character slice is still a newline or ESC in the rendered line.
    """
    text = _one_line(str(name or ""))
    if text.startswith("claude-"):
        text = text[len("claude-"):]
    # Trimmed after the cut, not before it: a four-character cap lands mid-word as
    # often as not, and `jit-` reads as a truncation artefact rather than as a name.
    return text[:4].rstrip("-_.") or "?"


def _plugins_field(plugins, symbols, color=False):
    """`plug 4ok`, and the names of whatever is not (#512).

    The block this replaces spent 45 characters at the right-hand end of the line --
    `oss 0.12.0 ✓ · supe 0.49.0 ✓ · reme 0.21.0 ✓ · jit 0.5.0 ✓` -- to say, on almost every
    render, that there is nothing to do. What a reader needs from four plugins that are
    current is the number of them.

    **The count is what makes the collapse safe, and it is why this is not simply hidden
    when everything is fine.** ``plugin_facts`` argues the case for its own shape: a plugin
    absent because it is fine and a plugin absent because nothing looked at it render
    identically, and only the second is a problem. `4ok` says four were looked at and four
    answered; `plug ?` says nobody looked; and a plugin whose version could not be compared
    is neither, so it gets its own group rather than being counted current.

    Anything not current is named, because "one of these is behind" is not actionable
    without knowing which.
    """
    if not plugins:
        return "plug " + symbols["unk"]
    current = 0
    unknown = 0
    named = []
    for name, status in plugins:
        state = (status or {}).get("state")
        if state == "current":
            current += 1
            continue
        if state == "behind":
            marker = symbols["behind"].strip() + (_short_version(status.get("latest")) or "?")
            shade = YELLOW
        elif state == "ahead":
            marker = symbols["ahead"].strip() + (_short_version(status.get("installed")) or "?")
            shade = GREEN
        else:
            unknown += 1
            continue
        text = _short_name(name) + marker
        named.append(shade + text + RESET if color else text)
    count = "{}{}".format(current, symbols["ok"])
    parts = [GREEN + count + RESET if color and current else count]
    parts.extend(named)
    if unknown:
        text = "{}{}".format(unknown, symbols["unk"])
        parts.append(DIM + text + RESET if color else text)
    return "plug " + " ".join(parts)


def render(facts, ascii_only=False, color=False):
    """The whole line, from facts already gathered. No I/O, so it is testable.

    Colour is off by default because every assertion about this line is a string
    comparison; ``main`` turns it on.
    """
    symbols = _symbols(ascii_only)
    percent = facts.get("percent")
    if not isinstance(percent, (int, float)):
        context = "ctx ?"
    else:
        context = "{}%".format(int(percent))
        if color:
            shade = RED if percent >= 80 else YELLOW if percent >= 50 else GREEN
            context = shade + context + RESET
    model = facts.get("model")
    model = _one_line(str(model)) if model else "?"
    blocks = ["{}{}{}".format(model, symbols["dot"], context)]

    repo_name = facts.get("repo_name")
    repo_name = _one_line(str(repo_name)) if repo_name else "?"
    # The branch only when it is not the declared default (#509): in the clone that field
    # said `main` on every render, and this loop works in worktrees, so it cost width in
    # the one place it carried nothing and was identical in the place it carries news.
    # Silence here means "measured, and it is the default" -- so a branch git could not
    # report still renders `?`, and a config declaring no default has nothing to compare
    # against and renders the branch as before.
    #
    # Folded, which #493 deliberately declined to do here on the measured grounds that
    # `git check-ref-format --branch` refuses a newline and an ESC, so the value cannot
    # carry them. That measurement stands and is still asserted. The fold is kept anyway
    # for a reason that measurement does not cover: the comparison below is what decides
    # whether this field renders at all, and it compares `branch` against a value read out
    # of `.oss.json`, which git never vetted. Folding one side and not the other would
    # make two strings that differ only in a control character compare unequal and render
    # a branch that is the default. Both sides through the same funnel, and the property
    # test that treats every string-valued fact as untrusted then needs no exception here.
    branch = _one_line(facts["branch"]) if facts.get("branch") else "?"
    default = _one_line(facts["default_branch"]) if facts.get("default_branch") else None
    where = [repo_name]
    if default is None or branch != default:
        where.append(branch)
    if facts.get("version"):
        # This repo's own tracked manifest -- written by a contributor, not fetched
        # over the network, but still text this function did not produce itself.
        where.append("v" + _one_line(str(facts["version"])))
    blocks.append(" ".join(where))

    blocks.append(_board_field(facts.get("board") or {}, symbols, color))
    blocks.append(_release_field(facts.get("release")))
    blocks.append(_tick_field(facts.get("tick")))
    blocks.append(_last_field(facts.get("last")))

    blocks.append(_plugins_field(facts.get("plugins") or [], symbols, color))
    return symbols["sep"].join(blocks)


# ------------------------------------------------------------------------ gathering


def repo_root(start):
    path = Path(start).resolve()
    for candidate in [path] + list(path.parents):
        if (candidate / ".oss.json").is_file():
            return candidate
    return None


def repo_config(root):
    try:
        return json.loads((Path(root) / ".oss.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def repo_version(root):
    """The version this clone declares, or ``None``.

    The plugin manifest first, then the newest tag. Both are read rather than assumed,
    and a repo that states neither reports nothing rather than a guess.
    """
    manifest = Path(root) / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
        if version:
            return version
    except (OSError, ValueError):
        pass
    return _run(["git", "-C", str(root), "describe", "--tags", "--abbrev=0"]) or None


def branch_name(root):
    return _run(["git", "-C", str(root), "branch", "--show-current"]) or None


def _run(command, timeout=5):
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def plugins_root_default():
    return Path(os.path.expanduser("~")) / ".claude" / "plugins"


def _normalized_path(path):
    """Best-effort canonical form for comparing an installed-plugin ``projectPath``
    against the project actually being reported on. ``resolve()`` can raise on some
    platforms for a path with a permission problem partway up it -- fall back to a
    plain normalisation rather than letting a project-match check crash the caller.

    Passed through ``os.path.normcase`` on the way out: on Windows, whose filesystem is
    case-insensitive, the same directory can be named with two different cases -- an
    installed-plugin record and the path this session resolves are not guaranteed to
    agree on which -- and comparing case-sensitively would silently answer "no entry
    applies here" about a project whose entry is sitting right there. `normcase` folds
    case only on Windows (`ntpath`); on POSIX (`posixpath`, including macOS, whose
    default filesystem is also case-insensitive-but-preserving) it is the identity
    function, so this closes the gap measured on Windows and leaves the macOS one open
    -- worth a second pass, not claimed fixed here.
    """
    try:
        text = str(Path(path).resolve())
    except OSError:
        text = os.path.normpath(str(path))
    return os.path.normcase(text)


def _entry_applies(entry, project):
    """Does this ``installed_plugins.json`` entry govern ``project`` (#521)?

    ``scope`` of ``user`` (or, defensively, absent) applies everywhere this machine
    runs Claude Code. Anything else -- ``project``, ``local`` -- is restricted to the
    ``projectPath`` it names; with no ``project`` to compare against, or no
    ``projectPath`` on a restrictively-scoped entry, it matches nothing rather than
    being assumed to apply broadly, which is the collapse this fix exists to remove.
    """
    scope = entry.get("scope")
    if scope in (None, "user"):
        return True
    if project is None:
        return False
    entry_project = entry.get("projectPath")
    if not entry_project:
        return False
    return _normalized_path(entry_project) == project


def installed_plugins(project_root, plugins_root=None):
    """``{plugin name: {"version": ..., "repository": ...}}`` from the installed set,
    resolved for THIS project (#521).

    Derived from each plugin's own installed manifest rather than from a name-to-repo
    table here: a hardcoded map is a per-repo fact in shared code and is wrong the first
    time a plugin moves. Same derivation ``doctor.dependency_repositories`` uses.

    ``installed_plugins.json`` is one file shared by every project on this machine. One
    plugin has many entries -- one per scope and one per project that ever installed it
    -- and they carry different versions, because an old project's entry is never
    rewritten when a newer copy is installed elsewhere. The version this function used
    to report was the newest recorded *anywhere*, across every project -- which answers
    a question nobody asked: `max()` over the whole table can only ever report a version
    at or above the one actually resolved for this project, so a project pinned behind a
    sibling project's newer pin silently read as current (#521). Only entries that apply
    to ``project_root`` -- see `_entry_applies` -- are considered now; a project with no
    matching entry reports no version for that plugin, never the newest one lying
    around on the machine.
    """
    root = Path(plugins_root) if plugins_root is not None else plugins_root_default()
    try:
        doc = json.loads((root / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    project = _normalized_path(project_root) if project_root is not None else None
    found = {}
    for key, entries in (doc.get("plugins") or {}).items():
        name = key.split("@", 1)[0]
        for entry in entries or []:
            if not _entry_applies(entry, project):
                continue
            record = found.setdefault(name, {"version": None, "repository": None})
            version = entry.get("version")
            if version and version != "unknown":
                current = _version_tuple(record["version"])
                incoming = _version_tuple(version)
                if current is None or (incoming is not None and incoming > current):
                    record["version"] = version
            install_path = entry.get("installPath")
            if install_path and not record["repository"]:
                try:
                    manifest = json.loads(
                        (Path(install_path) / ".claude-plugin" / "plugin.json").read_text(
                            encoding="utf-8"
                        )
                    )
                except (OSError, ValueError):
                    continue
                record["repository"] = manifest.get("repository")
                record["dependencies"] = manifest.get("dependencies") or []
    return found


def repo_from_url(url):
    if not url:
        return None
    text = str(url).rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    parts = text.split("/")
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


def plugin_facts(loop_name, installed, latest_by_repo, stale=False):
    """The loop's own plugin and every dependency it declares, rendered alike.

    All of them, always, in one shape -- the set comes from the loop plugin's own
    manifest, so nothing here names a plugin and a new dependency arrives on the line
    without an edit. An earlier version showed only the ones that were not current,
    which reads well and is the wrong trade for this field: a plugin that is absent
    because it is fine and a plugin that is absent because nothing looked at it render
    identically, and only the second is a problem. Shown uniformly, the marker carries
    the difference -- current, behind (in the colour that means *update this*), ahead,
    or `?` for a comparison nobody could make.

    ``stale`` is one fact about the whole cached `latest_by_repo` reading -- it was
    fetched in one pass and carries one stamp (#550) -- so it applies uniformly to
    every plugin compared here rather than being asked per name.
    """
    mine = installed.get(loop_name) or {}

    def status_for(name):
        record = installed.get(name) or {}
        return version_status(
            record.get("version"),
            latest_by_repo.get(repo_from_url(record.get("repository"))),
            stale=stale,
        )

    facts = [(loop_name, status_for(loop_name))]
    for name in mine.get("dependencies") or []:
        facts.append((name, status_for(name)))
    return facts


# ------------------------------------------------------------------------- refresh


def _gh_count(repo, kind):
    """One exact count, read off the search API's own `total_count`.

    The alternative -- walking every page of results and counting rows client-side --
    runs its filter once per page and prints one number per page with no total, so
    whoever reads the first line gets a number smaller than the truth, correctly
    formatted, at exit 0. One call and one field cannot fail that way.
    """
    query = "repo:{} is:{} is:open".format(repo, kind)
    out = _run(
        [
            "gh",
            "api",
            "-X",
            "GET",
            "search/issues",
            "-f",
            "q=" + query,
            "-f",
            "per_page=1",
            "--jq",
            ".total_count",
        ],
        timeout=25,
    )
    try:
        return int(out)
    except (TypeError, ValueError):
        return None


#: How many open pull requests one rollup page carries. Anything past it is counted as
#: unknown rather than dropped, so the groups still sum to the exact count beside them.
ROLLUP_PAGE = 100


def _gh_rollups(repo):
    """One page of open pull requests with what CI says about each, or ``None``.

    A separate call from the counts above because the search API does not carry a check
    rollup. It is bounded, and the bound is visible in the output rather than silent: the
    remainder lands in the `?` group, which is what a page limit actually produced.
    """
    out = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(ROLLUP_PAGE),
            "--json",
            "number,statusCheckRollup",
        ],
        timeout=25,
    )
    if not out:
        return None
    try:
        rows = json.loads(out)
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def _latest_release(repo):
    """The version a plugin's own manifest declares on its default branch.

    **Not `releases/latest`, and the difference is not cosmetic.** A GitHub Release is a
    document somebody publishes; `claude plugin update` resolves the marketplace's source
    repository, so the manifest on the default branch is what would actually install.
    Measured: `claude-jit-context` carries tag `v0.5.0` and a latest *release object* of
    `v0.4.0`, so reading releases reported an install that is current as `ahead` -- a
    finding about a publication step, rendered in the column that means "your install is
    out of step".

    `doctor.published_versions` already asks this exact question this exact way. Two
    sources for one question is how a status line and a diagnostic come to disagree in
    front of the same person, which is worse than either being wrong alone.
    """
    if not repo:
        return None
    encoded = _run(
        [
            "gh",
            "api",
            "repos/{}/contents/.claude-plugin/plugin.json".format(repo),
            "--jq",
            ".content",
        ],
        timeout=25,
    )
    if not encoded:
        return None
    try:
        import base64

        return json.loads(base64.b64decode(encoded).decode("utf-8")).get("version")
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def refresh(root, now=None):
    """Fill the cache for one managed repository. Runs detached, never on the render path.

    Two clocks (#515). The board -- open pull requests, open issues, their check rollups --
    is re-read every time; the version each plugin's source repository publishes is re-read
    only when its own longer interval has passed, and carried forward from the previous
    cache in between. Four of the seven forge calls were the second kind, which is why the
    board's own interval could not be shortened while they shared one.

    A carried-forward value carries its own stamp with it. Stamping it `now` would make an
    hour-old reading indistinguishable from one just taken, which is the same defect this
    module spends the rest of its length avoiding.
    """
    now = time.time() if now is None else now
    root = Path(root)
    config = repo_config(root)
    repo = config.get("repo")
    previous = read_cache(cache_path(repo)) or {}
    document = {"fetched_at": now, "repo": repo}
    if repo:
        document["prs"] = _gh_count(repo, "pr")
        document["issues"] = _gh_count(repo, "issue")
        document["pr_checks"] = check_rollup_counts(_gh_rollups(repo), document["prs"])
    carried = previous.get("latest")
    carried = dict(carried) if isinstance(carried, dict) else {}
    carried_stamp = previous.get("latest_fetched_at")
    if not isinstance(carried_stamp, (int, float)):
        carried_stamp = previous.get("fetched_at")
    if not latest_is_due(previous, now):
        document["latest"] = carried
        document["latest_fetched_at"] = carried_stamp
    else:
        latest = {}
        answered = False
        for record in installed_plugins(root).values():
            slug = repo_from_url(record.get("repository"))
            if slug and slug not in latest:
                tag = _latest_release(slug)
                if tag:
                    latest[slug] = tag
                    answered = True
        if answered:
            document["latest"] = latest
            document["latest_fetched_at"] = now
        else:
            # Asked and got nothing back. A network that answered once and cannot now is
            # not a plugin with no published version, so the previous reading stays --
            # under its own old stamp, which is what makes it due again immediately.
            document["latest"] = carried
            document["latest_fetched_at"] = carried_stamp
    path = cache_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(document), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return document


def invalidate_latest_cache(repo, now=None):
    """Clear the cached `latest` reading for `repo`, because something just made it
    false (#549). `/oss:release` calls this immediately after the Release it just
    created makes the cached manifest-version reading stale -- the falsifying
    event, known at the moment it happens, rather than waited out on a clock that
    cannot see it (#550 covers the render side of the same incident; neither
    substitutes for the other).

    Three states, because a cache this could not reach and a cache with nothing to
    clear must not render alike:

    * ``invalidated`` -- a `latest` (or `latest_fetched_at`) entry existed and is
      now `{}` / a stamp one second past due, rather than absent. The next
      render or refresh starts from "nobody has asked yet" rather than from the
      value that was just falsified.
    * ``nothing-to-invalidate`` -- no cache file at this path, or one that carries
      no `latest` reading at all. There was nothing to falsify.
    * ``could-not-invalidate`` -- the file exists and could not be read, could not
      be parsed, is not a JSON object, or could not be written back. An absent
      directory, an unreadable file, or a different `XDG_CACHE_HOME` than the
      rendering session uses all land here rather than passing as either state
      above.

    **`latest`/`latest_fetched_at` are set to `{}`/well in the past, never
    deleted** -- this was a bare `pop()` of both keys and it was wrong (self-review
    finding on this same issue): `latest_is_due` reads a document with no
    `latest_fetched_at` at all as a legacy, pre-#515 cache and falls back to
    comparing `now` against `fetched_at` -- the BOARD's own stamp, refreshed on
    nearly every render. A document with both keys simply gone therefore reads as
    "recently fetched" the instant the next board refresh runs, and `refresh()`
    carries the (empty) `latest` forward under that fresh-looking stamp instead of
    re-asking, in an active session effectively forever.

    `latest_fetched_at` is stamped `now - LATEST_REFRESH_AFTER - 1` -- one second
    past due, relative to the moment of invalidation, rather than a fixed absolute
    sentinel like `0`. Anchoring to an absolute epoch would only be reliably "due"
    against a real wall clock (`now` several billion seconds past `0`), and this
    module's own test suite drives `now` with small synthetic values throughout
    (e.g. `1_000.0`); an absolute sentinel would be correct in production and
    silently wrong under exactly the convention this repository tests with. The
    relative stamp is due under `latest_is_due` regardless of what `now` means.

    ``now`` defaults to `time.time()`, matching `refresh()`'s own parameter, so a
    test can drive it without a real clock.

    Read-modify-write on the same file `refresh()` writes, with the same
    write-to-temp-then-`os.replace` -- a concurrent renderer's own read either sees
    the old document or the new one, never a half-written one. This never touches
    the `prs`/`issues`/`pr_checks` board half of the document; only the two
    `latest*` keys are the concern here.
    """
    now = time.time() if now is None else now
    path = cache_path(repo)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The ordinary case: no cache has been written for this repo yet, or the
        # rendering session uses a different `XDG_CACHE_HOME` and this process
        # cannot see what it wrote. `read_text` is asked directly rather than
        # `path.exists()` first -- `Path.exists()` swallows a version-dependent
        # set of `OSError` subclasses (this repo's own CLAUDE.md), so a genuine
        # miss and an unreadable path could otherwise fold into the same branch.
        # `FileNotFoundError` is the one exception this call can raise that means
        # "absent", unambiguously, on every supported version.
        return {"state": "nothing-to-invalidate", "detail": "no cache file at {0}".format(path)}
    except OSError as exc:
        return {
            "state": "could-not-invalidate",
            "detail": "{0} could not be read -- {1}: {2}".format(path, type(exc).__name__, exc),
        }
    try:
        document = json.loads(raw)
    except ValueError as exc:
        return {
            "state": "could-not-invalidate",
            "detail": "{0} did not parse -- {1}: {2}".format(path, type(exc).__name__, exc),
        }
    if not isinstance(document, dict):
        return {
            "state": "could-not-invalidate",
            "detail": "{0} is not a JSON object".format(path),
        }
    if "latest" not in document and "latest_fetched_at" not in document:
        return {
            "state": "nothing-to-invalidate",
            "detail": "{0} carries no `latest` reading".format(path),
        }
    # NOT a bare delete of both keys (self-review finding on this issue's own
    # implementation): `latest_is_due` reads a document with no `latest_fetched_at`
    # as a legacy, pre-#515 cache and falls back to comparing `now` against
    # `fetched_at` -- the BOARD's own stamp, refreshed on nearly every render. A
    # document produced by simply popping both keys therefore reads as "recently
    # fetched" the moment the next board refresh runs, and `refresh()` then carries
    # the (now-empty) `latest` forward under that fresh-looking stamp instead of
    # re-asking -- invalidation silently undoing its own purpose for up to another
    # full `LATEST_REFRESH_AFTER`, and in an active session (board refreshing
    # continuously) effectively indefinitely. Measured directly: with
    # `fetched_at` re-bumped every few hundred seconds and `latest_fetched_at`
    # deleted, `latest_is_due` returned `False` from ten seconds after invalidation
    # onward.
    #
    # Setting `latest_fetched_at` to `now - LATEST_REFRESH_AFTER - 1` -- one
    # second past due, relative to this call's own `now` -- rather than deleting
    # it keeps `isinstance(..., (int, float))` true, so `latest_is_due` takes its
    # ordinary (non-legacy) branch and compares against a stamp that is due by
    # construction, regardless of what the board's own `fetched_at` says. A fixed
    # absolute sentinel (`0`) was tried and rejected: it is due against a real
    # wall clock but not against the small synthetic `now` values this module's
    # own tests use throughout, which would make the fix correct in production and
    # silently untested (and untestable in the small-`now` convention) at once.
    # `latest` is set to `{}` rather than removed for the same reason: presence,
    # not absence, is what a reader (this module's own `gather()`, and #551's
    # `check_latest_skew`) should see as "nothing here yet", so the state is
    # explicit rather than inferred from a missing key two different callers
    # could read two different ways.
    document["latest"] = {}
    document["latest_fetched_at"] = now - LATEST_REFRESH_AFTER - 1
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(document), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as exc:
        return {
            "state": "could-not-invalidate",
            "detail": "{0} could not be written -- {1}: {2}".format(path, type(exc).__name__, exc),
        }
    return {"state": "invalidated", "detail": "cleared cached `latest` at {0}".format(path)}


def _lock_path(repo):
    return cache_path(repo).with_suffix(".lock")


def _fork_refresh(root, repo):
    """Start a detached refresh, at most one at a time.

    The lock carries a timestamp rather than being a directory: a refresher killed
    mid-run must not freeze the counts forever, so a stale lock is simply overwritten.
    """
    lock = _lock_path(repo)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_STALE_AFTER:
            return
        lock.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        return
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--refresh", "--root", str(root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------- main


def gather(payload, root, now=None):
    now = time.time() if now is None else now
    config = repo_config(root)
    cache = read_cache(cache_path(config.get("repo")))
    board = board_from_cache(cache, now=now)
    if board_is_due(cache, now):
        _fork_refresh(root, config.get("repo"))
    latest = (cache or {}).get("latest") or {}
    # `latest_fetched_at` used to be read here and dropped, so `plugin_facts` decided
    # `current`/`behind`/`ahead` with no knowledge of the reading's own age -- the
    # same defect `refresh()`'s docstring warns against, one function later (#550).
    # `latest_is_due` is the same threshold `refresh()` itself uses to decide whether
    # a reading needs asking again; a comparison this old is folded into `unknown`
    # rather than rendered as a real answer. It does NOT catch a reading that is
    # fresh by that same rule and simply wrong -- #549 closes that gap by
    # invalidating the cache at the moment a publish falsifies it.
    stale_latest = latest_is_due(cache, now)
    loop_name = os.environ.get("OSS_STATUSLINE_PLUGIN", "oss")

    # One tail read shared by both transcript-derived facts (#504) -- a render
    # happens on every message, so a second full scan would be a doubled,
    # unmeasured cost paid every time rather than an occasional one.
    lines, truncated, error = _scan_transcript(payload.get("transcript_path"), DEFAULT_TAIL_BYTES)
    if error is not None:
        tick = {"state": "unknown", "detail": error}
    else:
        tick = _next_tick_from_lines(lines, truncated, now=now)

    return {
        "model": ((payload.get("model") or {}).get("display_name") or "").split(" ")[0] or None,
        "percent": (payload.get("context_window") or {}).get("used_percentage"),
        "repo_name": Path(root).name,
        "branch": branch_name(root),
        "default_branch": config.get("default_branch"),
        "version": repo_version(root),
        "board": board,
        "release": git_release_progress(root),
        "tick": tick,
        "last": _render_stamp(now),
        "plugins": plugin_facts(loop_name, installed_plugins(root), latest, stale=stale_latest),
    }


def _console_sample():
    """Every symbol `render` can put on the line, concatenated once.

    Built from `_symbols(False)` rather than written out here (#535): the probe used to
    hardcode four of the seven symbols that set renders, so a symbol added to `_symbols`
    later -- as #508's two CI-group glyphs were -- was never probed at all. A codepage
    that encodes the old four but not the new ones would reach `sys.stdout.write` and
    raise `UnicodeEncodeError` after the line's work was already done.
    """
    return "".join(_symbols(False).values())


def _ascii_only(stream):
    """Does this console's encoding survive the symbols? Measured, not assumed.

    On Windows stdout carries the console codepage rather than the source encoding, so
    an arrow raises ``UnicodeEncodeError`` at the ``print`` -- after the work it was
    reporting already happened. Rather than table the platforms, encode a sample and look.
    """
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        _console_sample().encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--refresh" in argv:
        root = "."
        if "--root" in argv:
            root = argv[argv.index("--root") + 1]
        refresh(root)
        try:
            _lock_path(repo_config(root).get("repo")).unlink()
        except OSError:
            pass
        return 0

    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    start = (payload.get("workspace") or {}).get("current_dir") or os.getcwd()
    root = repo_root(start)
    if root is None:
        # Not a repository this loop manages. Say the little that is true rather than
        # rendering an OSS board about a repo that has none.
        model = ((payload.get("model") or {}).get("display_name") or "?").split(" ")[0]
        percent = (payload.get("context_window") or {}).get("used_percentage")
        sys.stdout.write(
            "{} {}".format(model, "?" if percent is None else "{}%".format(int(percent)))
        )
        return 0
    line = render(gather(payload, root), ascii_only=_ascii_only(sys.stdout), color=True)
    sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
