## Running tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

Integration tests (includes shell scripts and prompt validation):

```bash
bash scripts/run-tests.sh          # without Haiku
bash scripts/run-tests.sh --live   # with real Haiku call
```

### Skips print their reason

`addopts` carries `-rs`, so every skipped test prints *why* it skipped
([#306](https://github.com/Digital-Process-Tools/claude-remember/issues/306)).
That is not verbosity for its own sake. A skip here is a checker saying it could
not answer — the shell-parse gate naming the bash 3.2 constructs that went
unchecked because no floor interpreter was installed, the timestamp comparison
naming the `printf '%(...)T'` builtin this bash does not have — and rendered as
a bare `s` that sentence never reaches anyone. A green run with silent skips
looks exactly like a green run that checked everything.

Read the `SKIPPED` block at the end of a run before concluding a leg is covered.
On a Linux runner it is where you find out that the floor bash was not.

### A test that dominates the suite (`#510`)

`pytest` already prints `--durations` (`addopts` carries `--durations=25`);
nothing read it before #510, so one runaway test could grow to a large share
of every leg's wall-clock time and the only detector was a human happening to
scroll a CI log far enough to notice. The root-level `conftest.py` closes that:
a `pytest_terminal_summary` hook fires at the end of *every* `pytest`
invocation -- local or any leg of the matrix, no extra flag -- and prints the
top durations plus the slowest single test's **share of total suite time**
(a ratio, not an absolute second count, since an absolute count says more
about the runner than the test). The math lives in
`scripts/report_test_durations.py`, kept separate from the hook so it is
testable without driving a nested `pytest` session.

Three states, always distinguishable in the printed text:

- `measured` -- durations were collected and compared against
  `test-durations-baseline.json` at the repo root.
- `no-baseline` -- durations were collected but that file does not exist yet
  (true of every run until a maintainer records one by hand). The share is
  still printed, said out loud as `no-baseline` rather than rendered as if
  nothing had changed.
- `could-not-measure` -- no per-test durations could be collected, or the
  total suite time was not a usable number. Always names the reason; never
  prints nothing, which would read exactly like a suite with no hot test.

This is a report, never a gate: it does not fail a run on wall-clock time or
any share threshold, on purpose -- a shared CI runner's load is not in
anybody's diff, and a check that reddens a pull request for a neighbour's
noisy build only teaches people to re-run until green.

### The Windows leg's own skip ratio (`#497`, `#507`)

[#497](https://github.com/Digital-Process-Tools/claude-remember/issues/497) measured
the `windows-latest` legs reporting `success` while 1201 of 1960 collected tests --
roughly 61% -- skipped outright: most test modules blanket-skip on
`sys.platform == "win32"`, and only a handful use `tests/_bash_runner.py`'s
`resolve_bash()` route ([#432](https://github.com/Digital-Process-Tools/claude-remember/issues/432))
to actually run under Git Bash instead. A green leg that skipped most of the
suite reads exactly like a green leg that ran it -- this project's own named
defect class, at matrix scale.

The same `pytest_terminal_summary` hook that prints the #510 duration report
also prints a live skip ratio for the current leg, via
`scripts/report_windows_skip_floor.py`: `not-applicable` off Windows,
`under-floor` or `over-floor` against a recorded 10% floor on Windows
(the issue's own suggested starting point), or `could-not-measure` if the
counts it read off `terminalreporter.stats` were not usable numbers. Crossing
the floor also prints a `::warning::` GitHub Actions annotation, so the
`windows-latest` legs in the Actions Checks UI carry a visible flag rather
than a plain green tick.

**This is a report, not a gate, deliberately** -- the same choice #510 makes
for its own reporter, for a stronger reason here: today's actual ratio is
already far past any floor worth recording, so failing the build on it would
turn every future Windows leg red until the underlying modules are migrated
to `resolve_bash()`, a separate and much larger effort tracked by #497 itself.
The annotation makes the number impossible to miss without blocking work that
has nothing to do with it.

### Measuring the warm path (`tests/env_cache.py`)

`scripts/lib-env-cache.sh` refuses its cache unless the cache file is `-nt`
every config layer, and bash's `-nt` compares **whole seconds**. So a test that
writes a config and then counts process spawns on the "warm" run is measuring
one of two different things depending on which side of a second boundary the two
writes landed on — cold and expensive, or warm and cheap. Both are correct
product behaviour; only one is what such a test claims to measure
([#303](https://github.com/Digital-Process-Tools/claude-remember/issues/303)).

Write config layers through `tests.env_cache.write_config`, which backdates past
that granularity, and bracket the run being measured with an `EnvCacheProbe`:

```python
from tests.env_cache import EnvCacheProbe, write_config

write_config(home / ".remember" / "config.json", {"timezone": "UTC"})
_run(env)                                   # cold — publishes the resolution

probe = EnvCacheProbe(env["TMPDIR"])
probe.snapshot()
_run(env)                                   # the run being measured
probe.assert_warm("the spawn budget")
```

Backdating alone would only make the number *likely* to be right. The probe
makes the test **state which path it measured**, with the same three answers
everything else here gives: `warm`, `cold`, and `unknown` — the last meaning no
resolution was published or replayed, so the number cannot be attributed at all.
It needs no clock: a cold run ends in `_remember_env_cache_publish`, which
`mv`s a temp file over the cache, so the cache file's inode changes; a warm run
reads and writes nothing.

`tests.env_cache.invalidate` is the other direction, for a test that wants the
cache refused on purpose — and it needs the same whole-second margin, or the
config edit is invisible until the next second.

### The Python floor guard

The supported floor is **Python 3.9** — the lowest interpreter in the CI matrix
(`.github/workflows/tests.yml`). Syntax newer than the floor does not fail one
test, it fails *collection*, which takes out a whole matrix leg before anything
runs, and it is invisible on any machine with a newer Python (which is every
machine here).

`tests/test_pep604_floor_guard.py` catches that statically, on any interpreter,
in about a second. It flags PEP 604 unions (`str | None`) everywhere Python
evaluates them:

- parameter, return, and module- or class-level variable annotations, in files
  without `from __future__ import annotations`;
- `isinstance()` / `issubclass()` arguments — which the future import does
  *not* rescue, since those are ordinary runtime expressions;
- the type arguments of `cast()`, `NewType()` and `TypeVar()` (constraints and
  `bound=`), which are type positions by those callables' own contract;
- **bare assignments** at module or class level — `Handler = str | None` — but
  only when the discriminator below can tell them from bitwise arithmetic.

It runs in the normal suite; no 3.9 interpreter needs to be installed.

If it fails, the fix is `Optional[str]` from `typing`, or adding
`from __future__ import annotations` when the union is only in annotations.

**What it does not catch, and why.** `Handler = str | None` and
`MASK = READ | WRITE` are the same AST node, and nothing separates them
without type information. The guard flags an assignment only when some operand
*cannot* be bitwise-or'd on any Python — `None`, a builtin type name, a name
imported from `typing`, or a subscript of one. That is decided by the
language, not guessed, so it does not produce false positives on real bitwise
code. The price is the other direction: an alias over names it cannot resolve,
such as `Ids = A | B`, is **not** flagged and will still break a 3.9 leg. That
trade is deliberate — a guard people learn to ignore is worse than no guard.

Those cases are not silent. They come back as `GuardReport.undecided` and are
counted in the report's reason: seen, not classified, and not reported as
clean. Function-local assignments and the bodies of `if TYPE_CHECKING:` are out
of scope, because neither is evaluated when the module is imported.

