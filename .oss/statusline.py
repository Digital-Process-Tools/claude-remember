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
import re
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

#: A third clock (#613), beside the two above, for the one field that answers a
#: question neither of them can afford: is the watch channel actually delivering.
#: `channel:health` is classed `acts` and spawns `claude mcp get` once per
#: subscription tag -- 1-3s measured in supertool's own presets/watch/channel.py
#: -- so it cannot share the board's 60s clock, and the consumer it asks about can
#: die at any moment, which is far too often for LATEST_REFRESH_AFTER's 3600s.
#:
#: 300 is a GUESS TO BE MEASURED, not a number this module asserts as correct --
#: the issue's own words (#613). There is no history of consumer deaths on this
#: repository to fit an interval to, so any starting value ships unmeasured; what
#: would settle it is a wall-clock record of how long a real death goes
#: unreported at this interval, taken the first time one actually happens. State
#: what it cost when that reading is in hand -- do not silently promote this
#: constant to "measured" later without adding that record.
CHANNEL_REFRESH_AFTER = 300

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


# -------------------------------------------------------------------------- channel


#: The text after "channel: " on `channel:health`'s own first content line,
#: mapped to this module's five-way state (#613). Both routes to that report --
#: `channel.py` run directly and `supertool 'channel:health'` -- agree on this
#: text; only the exit code differs, and the supertool wrapper collapses every
#: non-zero exit to 1, so text is the only signal both routes share. Anything
#: not a key here -- an error page for a preset that is not enabled, output this
#: module has never seen -- is deliberately not in this table, so it falls
#: through to `cannot_determine` in `parse_channel_report` rather than being
#: guessed at.
CHANNEL_STATES = {
    "FORWARDING": "forwarding",
    "NOT DELIVERING": "not_delivering",
    "CANNOT DETERMINE": "cannot_determine",
    "CONTRADICTED": "contradicted",
    "BOUND, NOT SUBSCRIBED": "not_subscribed",
}

#: Same name supertool's own `presets/watch/naming.py` reads (`NAME_ENV`). Not
#: imported -- this module has no third-party imports and is vendored standalone
#: -- so the string is duplicated rather than the module.
WATCH_NAME_ENV = "SUPERTOOL_WATCH_NAME"

#: A copy of `oss_config.WATCH_NAME_UNSAFE_RE`'s substitution, not an import of
#: it, for the same reason `_one_line` below is a copy of `doctor.py`'s own
#: function rather than an import: this file is vendored into `.oss/statusline.py`
#: and must run standalone. `tests/test_statusline_channel_613.py` measures this
#: constant against `oss_config.watch_channel_name` directly so the two copies
#: cannot drift silently -- the failure mode #570 named for the supertool rule
#: body, one file over.
_WATCH_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

#: A copy of `oss_config.REPO_RE`, for the same standalone-vendoring reason as
#: the pattern above (#653). `oss_config.watch_channel_name` routes a candidate
#: `repo` through `repo_problem` -- this pattern -- BEFORE folding it, which is
#: what keeps a refused value like `'..'` from ever reaching the fold. The copy
#: above used to skip straight to the fold and had no refusal in front of it at
#: all, so `'..'` and `'../../etc'` -- both refused by `oss_config` -- still
#: produced a channel name here. `tests/test_statusline_watch_name_refusal_653.py`
#: is the positive control: slugs `oss_config` REJECTS, not only ones it accepts.
#: Excludes a backslash too, as of #897 -- `tests/test_statusline_watch_name_
#: refusal_653.py::test_repo_re_pattern_matches_oss_configs_own_pattern` pins the
#: two patterns together so this copy cannot drift from `oss_config.REPO_RE` again.
_REPO_RE = re.compile(r"\A[^/\\\s]+/[^/\\\s]+\Z")

#: `.supertool.json`'s own filename, read but never written -- the same constant
#: `doctor.py` carries as `WATCH_CONFIG`, duplicated rather than imported for the
#: same standalone-vendoring reason as every other copy in this section (#754).
WATCH_CONFIG = ".supertool.json"


def _expected_watch_name(repo):
    """The watch channel name THIS repository would derive from its own `repo`.

    `None` for anything that is not a non-empty string that also matches the
    same `owner/name` shape `oss_config.repo_problem` requires -- there is no
    name to expect from a `.oss.json` that states no repo, or one that states
    something `oss_config` would refuse, and `None` can never equal whatever
    `SUPERTOOL_WATCH_NAME` happens to hold, which is exactly the "do not
    attribute" outcome the issue's closing bullet asks for.
    """
    if not isinstance(repo, str) or not repo:
        return None
    if not _REPO_RE.match(repo):
        return None
    return _WATCH_NAME_UNSAFE_RE.sub("-", repo)


def _declared_watch_names(root):
    """The distinct `ops.*.watch_name` values in this repo's own `.supertool.json`
    (#754).

    A copy of doctor.py's own `_supertool_document`/`_declared_watch_names` read
    logic, not an import of it -- this module is vendored standalone (see the
    module docstring) and cannot depend on `doctor.py` being present beside it.

    Returns `(names, problem)`. `problem` is `None` when the file was read and
    its shape was usable -- which includes the file not being there at all,
    because absence is a real and common answer and a broken file is not.
    Otherwise `"unreadable"` (could not be opened, read or parsed as JSON) or
    `"malformed"` (parsed, and is not the object shape expected). doctor.py
    keeps those as two answers because its reader is sent to a different
    remedy for each; this caller only needs to know whether the file could
    answer the attribution question at all, so both fold to the SAME
    `declaration-unreadable` channel attribution in `_channel_reading` below --
    still its own state, and never folded into `not-attributable`, which is
    the fold #754 was filed against.
    """
    path = Path(root) / WATCH_CONFIG
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set(), None
    except (OSError, ValueError, UnicodeDecodeError):
        return set(), "unreadable"
    if not isinstance(doc, dict):
        return set(), "malformed"
    # Absent and malformed are not the same answer, and this asymmetry is the
    # one both review spawns on #754 caught in #754's own fix. `ops` missing
    # entirely is a repository that declares nothing -- real, common, and not a
    # problem. `ops` present and the wrong shape is a file somebody edited and
    # broke, and folding that into the first renders it as `not-attributable`
    # one caller down: identical to a channel that genuinely belongs to another
    # project's fleet, which is precisely the fold #754 exists to end.
    # doctor.py's own copy has split these since #216; this one had dropped the
    # branch while its docstring above claimed parity with it.
    if "ops" not in doc:
        return set(), None
    ops = doc.get("ops")
    if not isinstance(ops, dict):
        return set(), "malformed"
    # The empty-string filter is doctor.py's, kept here rather than dropped: a
    # block declaring `"watch_name": ""` declares no name, and counting it would
    # push a file that also declares one real name to len(declared) == 2 and so
    # to `not-attributable` -- a second, quieter way for a broken file to read
    # as somebody else's fleet.
    return {
        block["watch_name"]
        for block in ops.values()
        if isinstance(block, dict)
        and isinstance(block.get("watch_name"), str)
        and block["watch_name"]
    }, None


def parse_channel_report(text):
    """The state `channel:health` reported, from its own report text, or `None`.

    `None` covers everything that is not one of the five recognised states --
    most importantly the "op 'channel' is unavailable here" refusal supertool
    prints when the `watch` preset is not enabled, which also exits 1 and would
    otherwise be indistinguishable from a genuine `NOT DELIVERING` (#613; this
    was reproduced live against the installed supertool while filing this fix).
    A caller maps `None` to `cannot_determine`, never to a guess.
    """
    if not text:
        return None
    for line in str(text).splitlines():
        # Anchored at column 0, before any stripping (#654) -- an indented
        # line that merely LOOKS like a state line (e.g. a padded "channel  :"
        # detail line, or remote-authored text reproducing the shape) must
        # never outrank the genuine state line. Stripping first erased that
        # distinction and let the first STRIPPED match win regardless of
        # indentation, which relied on the report's own composition order
        # rather than on this parser's own anchor.
        if line.startswith("channel: "):
            return CHANNEL_STATES.get(line[len("channel: "):].strip())
    return None


def channel_status(raw_state, attribution, fetched_at, now, interval=CHANNEL_REFRESH_AFTER):
    """Fold a raw `channel:health` reading, its own age and its attribution into
    the state `render` actually shows (#613, widened by #754).

    Four ways this becomes `cannot_determine` before a caller ever sees one of
    the five real states, and each is a distinct reason a reader might act on
    differently -- collapsing them into one `?` would be this module's own
    defect class, the same reason `board_from_cache` keeps its counts separate:

    * ``not-asked``    -- nobody has taken a reading yet (`fetched_at` is None).
    * ``stale``        -- the reading is older than its own refresh interval
      (#550/#551's lesson, applied a third time: never let an old reading
      render as though it were fresh).
    * ``not-attributable`` -- the channel name this reading came from is
      neither what this repository's own `.oss.json` would derive NOR what
      this repository's own tracked `.supertool.json` declares, so the socket
      and poller slots may be another project's fleet entirely. Checked first
      and unconditionally: an unattributed reading must never reach the
      real-state branch below, however fresh it is.
    * ``declaration-unreadable`` -- neither route settled it, and the reason
      the declaration route could not is that `.supertool.json` is there and
      could not be read or parsed (#754). A file this module could not open
      is not evidence of "somebody else's fleet" -- it is evidence the
      question could not be asked, and folding the two together is precisely
      this repository's own defect class landing on the fix for it.

    `attribution` is one of ``"derivation"``, ``"declaration"``,
    ``"not-attributable"`` or ``"declaration-unreadable"`` -- `_channel_reading`
    decides which; this function only asks whether it is one of the first two
    (real attribution) or not.

    Deliberately NOT handled here, and this is #551's own gap restated for a
    third instrument: a reading that is fresh BY THIS RULE and simply wrong --
    the consumer died one second after the reading was taken -- renders exactly
    like a correct one. Nothing performs "the consumer died" the way
    `/oss:release` performs a publish, so there is no falsifying event to
    invalidate the cache against; #613's own docstring on `CHANNEL_REFRESH_AFTER`
    states that gap rather than papering over it.

    `not-asked` is checked BEFORE attribution, and that order is deliberate: a
    cache holding no reading at all also holds no attribution, so `attribution`
    defaults to a not-attributed value there too -- checking it first would
    report every never-asked repository as "not this repo's fleet" instead of
    "nobody has looked yet", which is a different and more alarming claim about
    a question that was never even put.
    """
    if not isinstance(fetched_at, (int, float)):
        return {"state": "cannot_determine", "reason": "not-asked"}
    if attribution == "declaration-unreadable":
        return {"state": "cannot_determine", "reason": "declaration-unreadable"}
    if attribution not in ("derivation", "declaration"):
        return {"state": "cannot_determine", "reason": "not-attributable"}
    if now - fetched_at >= interval:
        return {"state": "cannot_determine", "reason": "stale"}
    if raw_state not in CHANNEL_STATES.values():
        return {"state": "cannot_determine", "reason": "unrecognized"}
    return {"state": raw_state, "reason": None}


# ---------------------------------------------------------------------------- board


def board_from_cache(cache, now=None):
    """Read the forge counts back out of a cache document.

    Each count is read on its own. A cache written by a refresh where one call answered
    and another did not is a real state, and collapsing it to "unknown board" throws away
    the half that was measured -- which is why there is no summary `state` field here.
    An earlier version computed one (`unknown`/`partial`/`measured`) from exactly these
    same values, but nothing ever read it: `_board_field` renders `?` per missing value
    directly off `prs`/`issues`/`issues_external`/`checks`, regardless of what a summary
    said. #595 added a rule to that summary -- a cache carrying `issues` but no
    `issues_external` is `partial` -- and the rule could not affect anything a maintainer
    sees, because the field it was added to had no caller. That is the same shape as a
    check that never runs (#597): a guard nobody reads. Deleted rather than wired to a
    reader, because each of the three counts below already carries its own missing/present
    distinction, and a summary that can disagree with the values it summarizes is a second
    copy of the same fact -- one that was, in fact, never even complete: it never accounted
    for `checks` at all.
    """
    if not isinstance(cache, dict):
        return {"prs": None, "issues": None, "issues_external": None, "age": None}
    prs = cache.get("prs")
    issues = cache.get("issues")
    issues_external = cache.get("issues_external")
    prs = prs if isinstance(prs, int) else None
    issues = issues if isinstance(issues, int) else None
    issues_external = issues_external if isinstance(issues_external, int) else None
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
    return {
        "prs": prs, "issues": issues, "issues_external": issues_external,
        "checks": checks, "age": age,
    }


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
            "own": "b",
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
        # `BOUND, NOT SUBSCRIBED` (#613): a consumer that is bound, verified and
        # counting, with nobody subscribed -- distinct from both `ok` and `bad`,
        # because it is neither a pass nor an absence, it is a finding of its own
        # (supertool's own `presets/watch/channel.py` docstring: "a fourth state
        # on purpose", "a fifth state for the same reason"). Half-filled shape
        # reads as "handed off, half-heard" even before the colour is read.
        "own": "◐",
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
    """`4pr 2ok 1x 1... 0? . 23is / 2eis` -- how many are open, what CI says about each,
    and how many of the issues arrived from outside repository membership (#595).

    Lowercase because the fields either side of it are, and a status line that shouts one
    field trains the eye to read that one first regardless of what it says.

    **Every group renders, including the ones at zero.** A group that disappears when empty
    makes the reader subtract to find what is missing, and `0x` -- nothing red -- and `0...`
    -- nothing on the way -- are two of the more useful things this line can say. The one
    thing that does collapse is a reading that never happened: rollups nobody could fetch
    render as a single `?`, never as four zeros. `eis` follows the same rule: `0eis` is a
    real reading -- nobody outside has filed anything -- and it must stay visibly different
    from `?eis`, a count nobody could take, because zero external issues is both a common
    true answer and exactly what a failed call looks like.
    """
    prs = board.get("prs")
    issues = board.get("issues")
    issues_external = board.get("issues_external")
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
    return "{}pr {}{}{}is / {}eis".format(
        "?" if not isinstance(prs, int) else prs,
        groups,
        symbols["dot"],
        "?" if not isinstance(issues, int) else issues,
        "?" if not isinstance(issues_external, int) else issues_external,
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


def _channel_field(channel, symbols, color=False):
    """`ch` + a one-glyph verdict on the watch channel, or nothing at all (#613).

    Three or four characters -- the same width discipline `_plugins_field` (#512)
    argues for (that field spent 45 characters saying nothing on almost every
    render), scaled down for a field with five possible states rather than a
    per-plugin list.
    `None` -- never a placeholder `?` -- when `watch_channel` is off in
    `.oss.json`: an operator's deliberate off switch is not the same absence as
    a question this line asked and could not answer, and the whole point of the
    third state this repository is named after is keeping those apart.

    The five upstream states map to distinct markers because they call for
    distinct actions (the issue's own table): a pass, a definite negative, a
    finding that is neither, a contradiction, and "nothing was established".
    `CONTRADICTED` renders uncoloured on purpose, matching the issue's own table,
    whose shade column is blank for that row alone.

    **What this must never claim, in the render layer too, not only in the
    docstrings that compute the state:** `forwarding` means the consumer's own
    counters are moving, never that an event reached a Claude session --
    `channel:health`'s own module docstring states outright that delivery into a
    session is not observable from outside it. Nothing here spells `forwarded`
    as `delivered`.
    """
    if channel is None:
        return None
    state = channel.get("state")
    if state == "forwarding":
        text, shade = "ch" + symbols["ok"], GREEN
    elif state == "not_delivering":
        text, shade = "ch" + symbols["bad"], RED
    elif state == "not_subscribed":
        text, shade = "ch" + symbols["own"], YELLOW
    elif state == "contradicted":
        text, shade = "ch!", None
    else:
        text, shade = "ch" + symbols["unk"], DIM
    if not color or shade is None:
        return text
    return shade + text + RESET


#: `gh-branch`'s own four states, folded onto `_symbols`' four render glyphs (#856).
#: `"bad"` gets its own glyph -- a leg has actually failed, the one state that is a
#: finding rather than "not settled yet". `"running"` and `"no-run"` share `run` on
#: purpose: both mean "nothing to act on, look again later", and collapsing them is
#: the deliberate call the issue asked for rather than an oversight -- see
#: `_gh_default_branch_state`'s own docstring for why they are still told apart
#: before they reach this table, so `"no-run"` never silently becomes `"running"`.
_DEFAULT_BRANCH_GLYPH_KEY = {
    "green": "ok",
    "bad": "bad",
    "running": "run",
    "no-run": "run",
}


def _default_branch_marker(state, symbols, color=False):
    """One glyph, glued onto the repository name, for whether the default branch's
    head commit is green right now (#856) -- the reading nothing on this line said
    anything about, including the moment right after this loop's own merge, when
    the branch has a fresh commit and no concluded run yet.

    `None` -- rendering nothing, never `?` -- when `state` is `None`: either the
    config declares no default branch to compare against (a deliberate absence of
    the question, the channel field's own convention, #613), or `gather()` has
    already folded a stale reading into `"unknown"` before this function ever
    sees it, in which case `state == "unknown"` reaches here and DOES render --
    the `unk` glyph -- because a stale reading is a real answer ("we cannot
    currently say"), not the same absence as never having asked at all.

    Colour reinforces the glyph and is never its only carrier (#549/#550): every
    state below is a distinct shape in `_symbols`, monochrome or not.
    """
    if state is None:
        return None
    key = _DEFAULT_BRANCH_GLYPH_KEY.get(state, "unk")
    text = symbols[key]
    if not color:
        return text
    shade = {"ok": GREEN, "bad": RED, "run": YELLOW}.get(key, DIM)
    return shade + text + RESET


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
    # Glued onto the repo name with no separator, beside it rather than its own block
    # (#856) -- the identity block is already `name branch vversion`, one glance, and
    # this is one more glyph about the same subject rather than a fourth fact needing
    # its own width. `None` here means nothing rendered at all: see
    # `_default_branch_marker`'s own docstring for the two different reasons it can be.
    branch_marker = _default_branch_marker(facts.get("default_branch_state"), symbols, color)
    if branch_marker is not None:
        repo_name = repo_name + branch_marker
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
    channel_block = _channel_field(facts.get("channel"), symbols, color)
    if channel_block is not None:
        blocks.append(channel_block)
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


#: GitHub's own membership tiers -- `authorAssociation` values that mean "one of us".
_INSIDE_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _gh_external_issue_count(repo, total):
    """How many of the `total` open issues (`_gh_count`'s own answer) were filed by
    someone outside repository membership, per GitHub's `authorAssociation` (#595).

    Not `-author:@me`: that resolves to whoever is authenticated on this machine, so
    the count would be a fact about a laptop rather than about the repository, and a
    second maintainer running this same loop would see a different number for the
    same tracker. `authorAssociation` is repo-relative and identical for everyone --
    and it is already what supertool's own `gh-issues` op uses for its external-filer
    marker, so the two boards agree instead of answering differently.

    **Not `gh issue list --json authorAssociation` (#620).** That field has never
    existed on `gh issue list` -- `gh` refuses the whole call, exit 1, empty stdout,
    every single time, and the six-fixture suite that shipped with #595 could not
    catch it because every fixture there mocked `_run`'s *return value* and none of
    them looked at what `_run` was *called with*. `author_association` does exist on
    the REST `repos/{owner}/{repo}/issues` listing, so this reads that instead, with
    `--jq` selecting the one field this function needs and dropping pull requests --
    that endpoint mixes both, and a PR row carries a `pull_request` key an issue never
    has, so `select(.pull_request == null)` filters server-side before this function
    ever sees a row. Without it the row count would include PRs and permanently fail
    the cross-check below, because `_gh_count`'s own `is:issue` search never counts
    them.

    **`--jq` output, not `--paginate`'s raw concatenation, and the difference is not
    cosmetic.** `_gh_count`'s own docstring already documents that `gh api --paginate
    --jq` runs its filter once per page and prints one number per page with no total
    -- irrelevant here, since this asks for one row per issue rather than a count.
    What matters for `--paginate` *without* `--jq` is that each page is a raw JSON
    array, and concatenating two JSON arrays end to end produces text no parser can
    read (`[...][...]`) -- the exact trap #620's own writeup names for a naive fix.
    `--jq` sidesteps it by construction: piping `.[] | select(...) | .author_association`
    through jq's raw-output mode prints one bare `author_association` value per line,
    and *lines* concatenate safely across pages -- unlike JSON arrays, there is no
    boundary for two pages' lines to collide on. `--paginate` alone still walks every
    page regardless of the repository's issue count, so there is no analogue of the
    old `--limit`-at-100 hazard to reintroduce here.

    The row count is cross-checked against `total` exactly as before: fewer lines
    than the count `_gh_count` already took means this call did not cover every open
    issue (a rate limit, a truncated page, the tracker growing between the two
    calls), and the number is not reliable enough to report. `None`, never a count
    smaller than the truth -- the same convention `_gh_count`'s own docstring names.
    A `null` line (jq's raw-mode spelling of a missing/`None` field) is treated the
    same as a missing row: one unreadable association and the whole count is untaken.
    """
    if not isinstance(total, int):
        return None
    out = _run(
        [
            "gh",
            "api",
            "--paginate",
            "-X",
            "GET",
            "repos/{}/issues".format(repo),
            "-f",
            "state=open",
            "-f",
            "per_page=100",
            "--jq",
            ".[] | select(.pull_request == null) | .author_association",
        ],
        timeout=25,
    )
    if out is None:
        return None
    lines = out.split("\n") if out else []
    if len(lines) != total:
        return None
    external = 0
    for line in lines:
        assoc = line.strip()
        if not assoc or assoc.upper() == "NULL":
            return None
        if assoc.upper() not in _INSIDE_ASSOCIATIONS:
            external += 1
    return external


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


#: Concluded check-run outcomes read as a failure, for `_reading_from_check_runs`.
#: Matches `gh-branch`'s own red/benign split exactly (supertool's
#: `presets/_checks.py`: `FAILED_STATES` plus everything in `bucket()`'s
#: "other" catch-all except its own `BENIGN_STATES`) -- `startup_failure` is a
#: documented `conclusion` value (a run that failed before it could even
#: start) and belongs beside `failure`/`timed_out`, not omitted from it; a
#: `neutral`/`skipped`/`manual` conclusion is deliberately left out (a run
#: explicitly opting out of pass/fail is not a failure, the same call
#: `gh-branch`'s own `BENIGN_STATES` makes). Review found the omission of
#: `startup_failure` on this same round (#914).
_BAD_CHECK_RUN_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "cancelled", "stale",
     "startup_failure"}
)


def _reading_from_check_runs(repo, branch):
    """One raw reading off the check-runs endpoint (#914): total entries, and
    whether any of them are failed / still in flight / passed.

    GitHub Actions writes check-runs, not legacy commit statuses -- on an
    Actions-only repository the combined-status endpoint's `total_count` is `0`
    on every commit, always, which is #914's whole defect. This is the source
    that actually carries Actions data.

    Returns ``None`` when the call did not answer or produced something this
    function cannot parse -- never confused with a reading that genuinely came
    back empty, which is a dict with ``total == 0`` and every flag ``False``.
    """
    out = _run(
        [
            "gh",
            "api",
            "-X",
            "GET",
            "repos/{}/commits/{}/check-runs".format(repo, branch),
            "-f",
            "per_page=100",
            "--jq",
            "{total: .total_count, "
            "entries: [.check_runs[] | {status: .status, conclusion: .conclusion}]}",
        ],
        timeout=25,
    )
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    total = data.get("total")
    entries = data.get("entries")
    if not isinstance(total, int) or not isinstance(entries, list):
        return None
    # `gh api` does not auto-paginate. `per_page=100` covers the ordinary case, but a
    # commit with more check-runs than that still has a truncated `entries` here while
    # `total` (read straight from `.total_count`) stays correct -- the same "counted
    # right, read wrong" shape `_gh_external_issue_count` already guards against one
    # function over. Read as `None` (could not look) rather than scanning a partial
    # page and guessing "green" from entries that happen to all be `success`.
    if len(entries) != total:
        return None
    bad = False
    running = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") in ("queued", "in_progress"):
            running = True
            continue
        if entry.get("conclusion") in _BAD_CHECK_RUN_CONCLUSIONS:
            bad = True
    # Conjunctive, matching `gh-branch`'s own `verdict()`: green is "nothing
    # failed and nothing is still moving", not "at least one entry said
    # success". A commit whose only check-runs are `skipped`/`neutral` is
    # GREEN there too (review found this divergence on this same round,
    # #914) -- a per-entry `conclusion == "success"` requirement would read
    # that same commit as neither green nor bad nor running (it has no
    # `"success"` entry either) and fall through to `None`, an unknown that
    # `gh-branch` does not share.
    green = total > 0 and not bad and not running
    return {"total": total, "bad": bad, "running": running, "green": green}


def _reading_from_combined_status(repo, branch):
    """The same shape as `_reading_from_check_runs`, off the legacy
    combined-status endpoint (`repos/{repo}/commits/{ref}/status`).

    Kept alongside the check-runs reading rather than replaced by it (#914): a
    repository could carry legacy commit statuses posted by an external CI with
    no GitHub Actions runs at all, and this is the only source that would ever
    see those. Neither source alone can answer `"no-run"` on its own -- see
    `_gh_default_branch_state`, which is where the two readings are merged.

    `error` is read the same as `failure`. GitHub's own docs give the COMBINED
    summary's top-level `state` a three-value enum -- `failure`/`pending`/`success`
    -- so this is a defensive extra rather than a documented fourth value: `error`
    is the spelling an INDIVIDUAL entry in the legacy `statuses[]` array can carry,
    one level below what this function's own `--jq` filter reads. Kept anyway,
    because a top-level `state` outside its documented enum is exactly the shape
    an undocumented API change would take, and reading it as bad -- rather than
    falling through to `None`, which the merge below reads as "this source did
    not answer" -- is the conservative direction to guess wrong in.
    """
    out = _run(
        [
            "gh",
            "api",
            "repos/{}/commits/{}/status".format(repo, branch),
            "--jq",
            "{state: .state, total: .total_count}",
        ],
        timeout=25,
    )
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    total = data.get("total")
    if not isinstance(total, int):
        return None
    state = data.get("state")
    return {
        "total": total,
        "bad": state in ("failure", "error"),
        "running": state == "pending" and total > 0,
        "green": state == "success",
    }


def _gh_default_branch_state(repo, branch):
    """Is the default branch's head commit green? One of the four states `gh-branch`
    itself answers, read off two cheaper calls (#856, and #914 for the second one).

    `gh-branch` (supertool) enumerates every workflow run on the head SHA and
    collapses re-runs and multi-run workflows to answer conjunctively -- machinery
    this render does not need, because it produces one glyph, not a table.

    **Two sources, not one (#914).** The combined-status endpoint alone answers
    `total_count == 0` on every commit of an Actions-only repository, because
    GitHub Actions writes check-runs, not legacy commit statuses -- so a repo
    whose CI is entirely Actions (this one) could never read anything but
    `"no-run"` off it. Check-runs alone has the opposite gap: a repository
    carrying legacy statuses from an external CI, with no Actions runs at all,
    would show up as empty there. Neither reading is read as authoritative on
    its own; both are taken and merged below.

    Returns ``"green"``, ``"bad"``, ``"running"``, ``"no-run"``, or ``None`` when
    the branch or repo is not configured, or the call did not answer.

    **Merge order is bad, then running, then green, then no-run/None** -- the
    same "worst wins" shape `gh-branch` itself uses across workflows. A failure
    on either source is a failure; a leg still in flight on either source means
    "not settled" even if the other source is quiet; `"no-run"` is reserved for
    the case both sources answered and both came back with `total == 0`. If one
    source did not answer at all (`None`) and the other came back empty, this is
    read as `None` rather than guessed as `"no-run"` -- the unanswered source
    could carry data this function never saw, and `None` is what every caller
    here already reads as "no answer", never as "confirmed idle" (mirrors the
    `total_count == 0` vs `state` distinction the combined-status reading always
    made, one level up: an absence this function produced must not render as an
    absence on the branch).
    """
    if not repo or not branch:
        return None
    check_runs = _reading_from_check_runs(repo, branch)
    status = _reading_from_combined_status(repo, branch)
    if check_runs is None and status is None:
        return None
    if (check_runs and check_runs["bad"]) or (status and status["bad"]):
        return "bad"
    if (check_runs and check_runs["running"]) or (status and status["running"]):
        return "running"
    if (check_runs and check_runs["green"]) or (status and status["green"]):
        return "green"
    if check_runs is not None and status is not None:
        if check_runs["total"] == 0 and status["total"] == 0:
            return "no-run"
    return None


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


def _watch_preset_declared(root):
    """Does this repository's tracked `.supertool.json` enable the `watch` preset?

    Three states, not two: `True`/`False` are a real answer, `None` is "could not
    tell" -- no such file, or one this process could not parse -- and a caller
    must not spend the `channel:health` subprocess's own cost finding out the
    hard way. Measured live while filing #613: with the preset disabled,
    `supertool 'channel:health'` still exits 1 but prints "op 'channel' is
    unavailable here", never a `channel: ` line at all -- so skipping the call
    here also avoids relying on `parse_channel_report` to catch that refusal
    every single time.
    """
    try:
        data = json.loads((Path(root) / ".supertool.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        # Self-review finding on this issue: `.supertool.json` is a file this
        # module does not own or control the shape of, and `json.loads` accepts
        # any valid JSON document -- a bare list, a number, `null`. `.get` on
        # anything but a dict raised `AttributeError` here with no `except`
        # above it in `refresh()`'s own call chain, so a malformed-but-parseable
        # file silently killed the WHOLE detached refresh, not only this field:
        # board counts and plugin versions stopped updating along with it.
        return None
    presets = data.get("presets")
    if not isinstance(presets, list):
        return False
    return "watch" in presets


def _run_channel_health(timeout=30):
    """The raw text of `supertool 'channel:health'`, regardless of its exit code.

    NOT `_run`: that helper returns `None` on any non-zero exit, and `NOT
    DELIVERING`/`CANNOT DETERMINE`/`CONTRADICTED`/`BOUND, NOT SUBSCRIBED` are
    all real, distinct findings that exit non-zero on purpose (supertool's own
    `presets/watch/channel.py`: "a single non-zero would put answers this op
    exists to separate back into one bucket"). Using `_run` here would fold
    four of the five real states into the same `None` a missing binary
    produces, which is the exact defect this field exists to stop happening to
    the loop's own instrumentation.

    30s, not the 1-3s the issue's own measurement names: that number is the
    ordinary case, and `MCP_LOOKUP_BUDGET` plus `PS_TIMEOUT` (supertool's own
    constants) put a documented worst case north of 20s when a lookup is slow
    rather than merely present.
    """
    try:
        result = subprocess.run(
            ["supertool", "channel:health"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode("utf-8", "replace")


def _channel_reading(root, config):
    """One `channel:health` reading for `refresh()`, or why there is none.

    `(raw_state, attribution)`. `raw_state` is `None` when the `watch` preset
    is not declared, `.supertool.json` could not be read, the `supertool`
    binary could not be run, or its report carried no recognisable `channel: `
    line -- every one of those folds to `cannot_determine` in `channel_status`,
    never to a guess. `attribution` is independent of all of that, and answers
    a SEPARATE question: is the channel name this reading came from actually
    this repository's own (#754, widening #613's own closing bullet). Two
    independent routes, checked in this order:

    * ``"derivation"`` -- THIS process's own `SUPERTOOL_WATCH_NAME` -- read
      here, in the detached refresh's own environment, which is the
      environment the `channel:health` call below actually ran under -- is
      the name `_expected_watch_name` would derive from this repository's own
      `.oss.json`. The original and, before #754, the only route.
    * ``"declaration"`` -- derivation did not match, and a repository whose
      derived name exceeds supertool's own 32-character path-component cap
      MUST declare a shorter one in its own tracked `.supertool.json` to use
      the channel at all (#754's own filing), which makes derivation
      permanently unsatisfiable for it. A name THIS repository's own tracked
      file declares is attributable to this repository by the same evidence
      `doctor.py` already accepts when it reports "declared in
      .supertool.json and exported as SUPERTOOL_WATCH_NAME, and they match" --
      so this route asks the identical question: exactly one
      `ops.*.watch_name` declared (more than one is `not-attributable`, the
      same as doctor.py's own `conflict` state), and it equals what is
      actually exported.
    * ``"declaration-unreadable"`` -- derivation did not match, and
      `.supertool.json` exists and could not be read or parsed. Never folded
      into `not-attributable`: a file this module could not open is evidence
      the question could not be asked, not evidence of someone else's fleet.
    * ``"not-attributable"`` -- neither route matched (or nothing is
      declared, or nothing is exported). The socket and poller slots may be
      another project's fleet entirely, however real the reading itself is.

    Declaration is checked only when derivation did not already settle the
    question, so a repository that derives cleanly never pays for reading a
    second file it does not need.
    """
    expected = _expected_watch_name(config.get("repo"))
    actual = os.environ.get(WATCH_NAME_ENV) or None
    if bool(expected) and expected == actual:
        attribution = "derivation"
    else:
        declared, problem = _declared_watch_names(root)
        if problem:
            attribution = "declaration-unreadable"
        elif actual is not None and len(declared) == 1 and next(iter(declared)) == actual:
            attribution = "declaration"
        else:
            attribution = "not-attributable"
    if _watch_preset_declared(root) is not True:
        return None, attribution
    return parse_channel_report(_run_channel_health()), attribution


def refresh(root, now=None):
    """Fill the cache for one managed repository. Runs detached, never on the render path.

    Two clocks (#515), soon three (#613). The board -- open pull requests, open issues,
    who filed each, their check rollups -- is re-read every time; the version each
    plugin's source repository publishes is re-read only when its own longer interval
    has passed, and carried forward from the previous cache in between. Four of the
    eight forge calls are the second kind (#595 added a fourth board call), which is
    why the board's own interval could not be shortened while they shared one.

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
        document["issues_external"] = _gh_external_issue_count(repo, document["issues"])
        document["pr_checks"] = check_rollup_counts(_gh_rollups(repo), document["prs"])
        # Same call group, same `fetched_at`, same `stale_after` (#856): the default
        # branch's own CI state is exactly as time-sensitive as the pull-request board
        # it sits beside, and it shares the moment (a merge or an issue close in this
        # session) that already invalidates the rest of the board (#516). A second,
        # independent clock for one more field would be the interval this repository's
        # own history already argues against (#515's own reasoning, one field over).
        # `None` (no default branch configured, or the call did not answer) is a real
        # value here, not skipped -- `gather()` reads it back and a missing key would
        # be indistinguishable from a cache written before this field existed, which
        # is exactly the ambiguity `pr_checks`' own `isinstance` guard exists to avoid.
        # `_gh_default_branch_state` itself already answers `None` for an unconfigured
        # `default_branch`, so no separate `if` is needed here.
        document["default_branch_state"] = _gh_default_branch_state(
            repo, config.get("default_branch")
        )
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
    if config.get("watch_channel") is False:
        # A deliberate off switch (#613): no reading, no stamp, and never
        # carried forward from a previous `on` state -- `channel_status` reads
        # this back through `gather()` and the field disappears from the line
        # rather than rendering `?`, which would misstate a decision as a
        # question nobody could answer.
        document["channel"] = None
        document["channel_fetched_at"] = None
    else:
        previous_channel_stamp = previous.get("channel_fetched_at")
        channel_due = not isinstance(previous_channel_stamp, (int, float)) or (
            now - previous_channel_stamp >= CHANNEL_REFRESH_AFTER
        )
        if channel_due:
            raw_state, attribution = _channel_reading(root, config)
            document["channel"] = {"raw_state": raw_state, "attribution": attribution}
            document["channel_fetched_at"] = now
        else:
            # Carried forward under its OWN old stamp, same shape as `latest`
            # above and for the same reason: re-stamping `now` would make an
            # old reading indistinguishable from a fresh one at the render.
            document["channel"] = previous.get("channel")
            document["channel_fetched_at"] = previous_channel_stamp
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
    board_stale = board_is_due(cache, now)
    if board_stale:
        _fork_refresh(root, config.get("repo"))
    # Same fold `plugin_facts`/`version_status` already do for `latest` (#550), on
    # the same board clock `board_is_due` already computes above -- `default_branch`
    # itself present-but-unconfigured stays `None` (never asked, #613's own
    # convention), and a configured one whose reading has outlived `board_is_due`'s
    # own interval (or was marked stale by this session's own merge/close, #516)
    # folds to `"unknown"` rather than rendering whatever it last said. This is the
    # one field on this line where a stale `ok` is actively dangerous: #856's own
    # motivating case is the moment right after a merge, when the previous reading
    # is confidently green about a commit that no longer exists.
    default_branch_state = None
    if config.get("default_branch"):
        raw_branch_state = (cache or {}).get("default_branch_state")
        if board_stale or raw_branch_state not in ("green", "bad", "running", "no-run"):
            default_branch_state = "unknown"
        else:
            default_branch_state = raw_branch_state
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

    channel = None
    if config.get("watch_channel") is not False:
        raw_channel = (cache or {}).get("channel") or {}
        # `attribution` (a 4-valued string, #754) replaced `attributable` (a
        # bool, #613) in the cache document. A cache written by the older code
        # carries only the old key, so this `.get` falls to its default and one
        # refresh interval's worth of readings render `ch?` on the single
        # upgrade that crosses #754 -- self-healing at the next `refresh()`,
        # which rewrites the whole entry. Written down rather than migrated:
        # translating the old bool would mean asserting WHICH route attributed
        # a reading this code did not take, and `derivation` was only ever true
        # by inference. Naming a bounded, self-healing window beats inventing a
        # provenance for a value that no longer has one.
        channel = channel_status(
            raw_channel.get("raw_state"),
            raw_channel.get("attribution", "not-attributable"),
            (cache or {}).get("channel_fetched_at"),
            now,
        )

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
        "channel": channel,
        "default_branch_state": default_branch_state,
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
