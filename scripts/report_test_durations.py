"""Report the shape of the suite's time -- top durations, and the slowest
single test's share of total suite time (#510).

`pytest` already prints `--durations` (wired via `addopts` in
`pyproject.toml`); nothing read it before this, so a single test could grow
to dominate every leg of the matrix and the only detector was a human
scrolling a CI log by chance. See #510's own worked example: a test measured
at 43.92s against ~6.5s for the next slowest, roughly a fifth of the whole
suite, found only because a maintainer happened to read that far.

This module is deliberately **not** a text parser for pytest's own
`--durations` output. `conftest.py`'s `pytest_terminal_summary` hook calls
`collect_from_reports` with the same `TestReport` objects pytest's own
`-r`/`--durations` summary is built from, straight off `terminalreporter`,
which is more robust than re-parsing formatted text (no locale/width
surprises, no risk of pytest changing its own output format underneath a
regex) and is exercised automatically on every `pytest` invocation -- local
or CI -- with no extra flag.

Reports a percentage **share** of total suite time rather than an absolute
second count, deliberately: an absolute count says more about the runner a
leg happened to land on than about the test (#510).

Three states, and the third is the point -- a step that silently prints
nothing when it cannot measure is indistinguishable from a suite with no hot
test, which is the defect class this loop is named after, reappearing inside
the detector built to prevent it:

- **measured** -- durations were collected, a baseline file was found, and
  the share is compared against it.
- **no-baseline** -- durations were collected but no baseline file exists
  (first run, or nobody has recorded one yet). The share is still reported,
  said out loud as "no-baseline" rather than rendered as if there were no
  change.
- **could-not-measure** -- no per-test durations could be collected, or the
  total suite time is not a usable number. Always renders a non-empty
  message naming the reason.

This never fails a run on a wall-clock threshold or a share crossing any
number -- it is a report a human reads, never a gate (#510's "what NOT to
add").
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TOP_N = 10

# A sibling of this file rather than something CI writes back: nobody commits
# it automatically, so its plain absence is exactly the "first run" case the
# no-baseline state exists for. A maintainer who wants the "measured" state
# populates it by hand from a report they trust.
DEFAULT_BASELINE_PATH = Path(__file__).resolve().parent.parent / "test-durations-baseline.json"


def collect_from_reports(reports: Iterable[object]) -> dict:
    """Sum `setup`/`call`/`teardown` durations per test node id.

    `reports` is any iterable of objects carrying `.nodeid` and `.duration`
    -- pytest's own `TestReport`, or a stand-in used in tests. A report
    missing either attribute, or carrying a `.duration` that will not parse
    as a float, is skipped rather than raising: a hook that raises takes the
    whole test run down with it, and one malformed report must not cost
    every other test's duration data.
    """
    totals: dict = {}
    for report in reports:
        nodeid = getattr(report, "nodeid", None)
        duration = getattr(report, "duration", None)
        if nodeid is None or duration is None:
            continue
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            continue
        totals[nodeid] = totals.get(nodeid, 0.0) + duration
    return totals


@dataclass
class DurationReport:
    state: str  # "measured" | "no-baseline" | "could-not-measure"
    top: list = field(default_factory=list)  # list[tuple[str, float]]
    slowest_nodeid: str | None = None
    slowest_seconds: float | None = None
    total_seconds: float | None = None
    share: float | None = None
    baseline_share: float | None = None
    baseline_error: str | None = None
    reason: str | None = None  # populated for could-not-measure


def analyze(
    durations: dict,
    total_seconds: float | None,
    baseline: dict | None,
    top_n: int = DEFAULT_TOP_N,
) -> DurationReport:
    """Turn collected per-test durations into one of the three states.

    `durations` absent or empty, or `total_seconds` not a usable positive
    number, is `could-not-measure` -- explicitly, with a reason, rather than
    a report with nothing in it. Otherwise the slowest single test's share
    of `total_seconds` is computed, and the state is `measured` when
    `baseline` (a dict carrying `slowest_share`) is given, `no-baseline`
    when it is `None`.
    """
    if not durations:
        return DurationReport(
            state="could-not-measure",
            reason="no per-test durations were collected",
        )
    if total_seconds is None or total_seconds <= 0:
        return DurationReport(
            state="could-not-measure",
            reason=f"total suite time is not a usable number ({total_seconds!r})",
        )

    ranked = sorted(durations.items(), key=lambda kv: kv[1], reverse=True)
    slowest_nodeid, slowest_seconds = ranked[0]
    share = slowest_seconds / total_seconds

    baseline_share = None
    if baseline is not None:
        baseline_share = baseline.get("slowest_share")

    state = "measured" if baseline_share is not None else "no-baseline"
    return DurationReport(
        state=state,
        top=ranked[:top_n],
        slowest_nodeid=slowest_nodeid,
        slowest_seconds=slowest_seconds,
        total_seconds=total_seconds,
        share=share,
        baseline_share=baseline_share,
    )


def format_report(result: DurationReport) -> str:
    """Render `result` as human-readable text. Never returns an empty or
    blank string for any state -- a caller that prints this verbatim always
    prints *something*, which is the whole point of the could-not-measure
    state existing as a named branch rather than a silent no-op."""
    lines = ["-- test duration report (#510) --"]

    if result.state == "could-not-measure":
        lines.append(f"state: could-not-measure ({result.reason})")
        return "\n".join(lines)

    lines.append(f"state: {result.state}")
    lines.append(f"slowest test: {result.slowest_nodeid} ({result.slowest_seconds:.2f}s)")
    lines.append(f"total suite time: {result.total_seconds:.2f}s")
    lines.append(f"slowest share of total: {result.share:.1%}")

    if result.baseline_error:
        lines.append(f"baseline: ignored -- {result.baseline_error}")

    if result.state == "no-baseline":
        lines.append("baseline: none recorded -- reported with nothing to compare against")
    else:
        delta = result.share - result.baseline_share
        lines.append(f"baseline share: {result.baseline_share:.1%} (delta {delta:+.1%})")

    lines.append(f"top {len(result.top)} durations:")
    for nodeid, seconds in result.top:
        lines.append(f"  {seconds:8.2f}s  {nodeid}")

    return "\n".join(lines)


def load_baseline(path: Path):
    """Read a baseline file, returning `(baseline_dict_or_None, error_or_None)`.

    A missing file is the expected "first run" case: `(None, None)`, no
    error -- that is what turns into the `no-baseline` state, not a defect.
    A file that exists but will not parse, or is missing the field this
    module reads, is `(None, <reason>)`: also folds into `no-baseline` (a
    baseline this module cannot read is not usable, and refusing to fall
    back would turn a baseline problem into a wall-clock-shaped failure this
    issue explicitly rules out), but the reason is carried through so
    `format_report` can say the baseline was ignored, rather than reporting
    the same "none recorded" text a genuinely first run would produce.
    """
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} could not be read as JSON ({exc})"
    if not isinstance(data, dict) or "slowest_share" not in data:
        return None, f"{path} is missing the 'slowest_share' field"
    return data, None


def save_baseline(path: Path, result: DurationReport) -> None:
    """Write the current `measured`/`no-baseline` result as the new baseline.

    Not called automatically by anything in this repo -- no CI step commits
    a baseline back, on purpose, since that would need write access to the
    branch a pull request runs on, and no `--save-baseline` CLI ships in
    this file either. A maintainer who wants the `measured` state going
    forward calls this directly, once they trust a given run's number:

        python3 -c "
        from scripts.report_test_durations import analyze, save_baseline, DEFAULT_BASELINE_PATH
        result = analyze(durations, total_seconds, baseline=None)
        save_baseline(DEFAULT_BASELINE_PATH, result)
        "

    or from a `pytest` script/REPL session where `durations` and
    `total_seconds` are already in hand.
    """
    if result.slowest_nodeid is None or result.share is None:
        raise ValueError("cannot save a baseline from a could-not-measure result")
    path.write_text(
        json.dumps(
            {"slowest_nodeid": result.slowest_nodeid, "slowest_share": result.share},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
