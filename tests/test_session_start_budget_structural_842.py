"""thresholds.session_start_max_bytes must drop sections STRUCTURALLY (#842, review of #845).

The first cut of the budget found each memory section by searching the whole
captured SessionStart body for its "--- <basename> ---" header and cutting
around the first match with `${text%%"$h"*}` / `${text#*"$h"}`. Two defects
followed from that one shape:

1. Time. Those expansions cost time that grows with the SQUARE of the body
   size in bash 3.2 and 5 (one `${t#*"$h"}` measured at ~5s for 80 KB and
   ~72s for 320 KB). They ran once per dropped section and only on an
   over-budget body -- i.e. precisely on a large store, where SessionStart
   could hang or time out.

2. Integrity. The handoff block comes BEFORE the memory section and is file
   content (written by the model, or planted -- the #721 threat model). A line
   reading "--- archive.md ---" inside it was the FIRST match, so the cut
   landed inside the handoff: it removed the handoff's tail, its random-token
   "=== END LAST HANDOFF <tok> ===" fence and the "=== MEMORY ===" line, and
   then listed a file as dropped whose content was in fact still injected.
   A memory file is content in the same way, so the same holds one section
   later (a header-shaped line inside now.md).

The bar, per test: would it still pass if the code did nothing? Every
negative is paired with a positive control.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

FROZEN_TODAY = "2099-05-17"
TODAY_FILE = "today-" + FROZEN_TODAY + ".md"
DROPPABLE = ("archive.md", TODAY_FILE, "recent.md", "now.md")
NOTICE = "--- not injected (over thresholds.session_start_max_bytes)"

# Generous on purpose: the fixed render of a ~300 KB store takes well under a
# second; the quadratic cut it replaces takes minutes. The bound only has to
# separate those two on the slowest CI leg, not measure the fast one.
HANG_BOUND_SECONDS = 30


def _shim_date(bindir: Path) -> None:
    bindir.mkdir(exist_ok=True)
    shim = bindir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y-%m-%d" ]; then\n'
        f"  echo {FROZEN_TODAY}\n"
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    shim.chmod(0o755)


def _store(tmp_path: Path, bodies: dict, config: dict | None = None):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    for name, body in bodies.items():
        (remember / name).write_text(body, encoding="utf-8")
    merged: dict = {"features": {"plugin_promos": False}}
    if config:
        merged.update(config)
    (remember / "config.json").write_text(json.dumps(merged), encoding="utf-8")
    bindir = tmp_path / "bin"
    _shim_date(bindir)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_NO_PRINTF_T": "1",
        "REMEMBER_CONFIG_CACHE": "0",
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
    }
    return env, remember


def _start(env: dict, timeout: float = 120) -> tuple[str, float]:
    payload = json.dumps({
        "session_id": "cccccccc-0000-4000-8000-000000000845",
        "transcript_path": "/does/not/matter/845.jsonl",
        "hook_event_name": "SessionStart",
        "cwd": "/does/not/matter",
        "source": "startup",
    })
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            ["bash", str(SESSION_START)], env=env, input=payload,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"SessionStart did not finish within {timeout}s on an over-budget store "
            f"-- the section drop is not linear in the body size"
        )
    elapsed = time.monotonic() - t0
    assert result.returncode == 0, result.stderr
    return result.stdout, elapsed


def _listed(out: str) -> list[str]:
    """Paths named under the budget notice -- the block the hook itself
    emits, one path per line until the blank line that closes it."""
    if NOTICE not in out:
        return []
    block = out.split(NOTICE, 1)[1].split("\n", 1)[1]
    names = []
    for line in block.split("\n"):
        if not line.strip():
            break
        names.append(line.split(" (", 1)[0])
    return names


def _marker(name: str) -> str:
    return "BODY-" + name.upper().replace(".", "-").replace("_", "-") + "-845"


def _bodies(filler_bytes: int) -> dict:
    out = {
        "identity.md": "IDENTITY-845\n",
        "core-memories.md": "CORE-845\n",
    }
    for name in DROPPABLE:
        out[name] = _marker(name) + "\n" + ("x" * 79 + "\n") * (filler_bytes // 80) + "END\n"
    return out


class TestLargeOverBudgetStoreIsLinear:
    """Finding 1: ~300 KB of memory over a 9000-byte budget must not hang."""

    def test_300kb_store_finishes_quickly_and_drops_every_droppable_section(self, tmp_path):
        env, remember = _store(tmp_path, _bodies(75_000))
        out, elapsed = _start(env, timeout=HANG_BOUND_SECONDS * 2)
        assert elapsed < HANG_BOUND_SECONDS, (
            f"SessionStart took {elapsed:.1f}s on a ~300 KB over-budget store"
        )
        # Control: the drop still happened, and correctly.
        for name in DROPPABLE:
            assert _marker(name) not in out, f"{name} was not dropped"
            assert str(remember / name) in _listed(out), f"{name} dropped but not listed"
        assert "IDENTITY-845" in out and "CORE-845" in out
        assert "=== MEMORY ===" in out
        assert len(out.encode("utf-8")) < 9000 + 1500

    def test_cache_hit_on_a_large_store_is_also_linear_and_correct(self, tmp_path):
        """The second start serves the MEMORY section from the start-context
        cache, not a live render -- the budget must hold on that path too."""
        env, remember = _store(tmp_path, _bodies(75_000))
        _start(env, timeout=HANG_BOUND_SECONDS * 2)
        out, elapsed = _start(env, timeout=HANG_BOUND_SECONDS * 2)
        assert elapsed < HANG_BOUND_SECONDS
        for name in DROPPABLE:
            assert _marker(name) not in out, f"{name} was not dropped on a cache hit"
            assert str(remember / name) in _listed(out)

    def test_only_what_does_not_fit_is_dropped(self, tmp_path):
        """Control: a store just over budget loses archive.md and nothing else."""
        bodies = _bodies(400)
        bodies["archive.md"] = _marker("archive.md") + "\n" + "a" * 9000 + "\n"
        env, remember = _store(tmp_path, bodies)
        out, _ = _start(env)
        assert _listed(out) == [str(remember / "archive.md")], out[-1500:]
        assert _marker("archive.md") not in out
        for name in (TODAY_FILE, "recent.md", "now.md"):
            assert _marker(name) in out, f"{name} dropped although archive alone sufficed"


HANDOFF_WITH_FAKE_HEADERS = (
    "HANDOFF-HEAD-845\n"
    "--- archive.md ---\n"
    "HANDOFF-MIDDLE-845\n"
    "--- now.md ---\n"
    "HANDOFF-TAIL-845\n"
)


class TestHandoffContentCannotSteerTheCut:
    """Finding 2: header-shaped lines inside the handoff are content."""

    def _run(self, tmp_path, now_filler: int):
        bodies = _bodies(400)
        bodies["archive.md"] = _marker("archive.md") + "\n" + "a" * 9000 + "\n"
        bodies["now.md"] = _marker("now.md") + "\n" + "n" * now_filler + "\n"
        bodies["remember.md"] = HANDOFF_WITH_FAKE_HEADERS
        env, remember = _store(tmp_path, bodies)
        out, _ = _start(env)
        return out, remember

    def _assert_handoff_intact(self, out):
        for piece in ("HANDOFF-HEAD-845", "HANDOFF-MIDDLE-845", "HANDOFF-TAIL-845"):
            assert piece in out, f"{piece} was cut out of the handoff.\n{out[:3000]}"
        fence = re.search(r"^=== END LAST HANDOFF \d+ ===$", out, re.MULTILINE)
        assert fence, f"the handoff's random-token fence was removed.\n{out[:3000]}"
        assert re.search(r"^=== MEMORY ===$", out, re.MULTILINE), f"=== MEMORY === was removed.\n{out[:3000]}"
        assert out.index("HANDOFF-TAIL-845") < fence.start() < out.index("=== MEMORY ===")

    def _assert_listing_matches_reality(self, out, remember):
        listed = set(_listed(out))
        for name in DROPPABLE:
            dropped = _marker(name) not in out
            assert (str(remember / name) in listed) == dropped, (
                f"{name}: listed={str(remember / name) in listed} but actually dropped={dropped}\n"
                f"{out[-2000:]}"
            )

    def test_fake_archive_header_in_handoff_is_not_where_the_cut_lands(self, tmp_path):
        out, remember = self._run(tmp_path, now_filler=100)
        self._assert_handoff_intact(out)
        self._assert_listing_matches_reality(out, remember)
        # Control: the REAL archive section is still the one dropped.
        assert _marker("archive.md") not in out
        assert str(remember / "archive.md") in _listed(out)

    def test_fake_now_header_in_handoff_survives_a_drop_reaching_now(self, tmp_path):
        """Big enough that now.md must go too -- the drop that would cut at
        the handoff's own '--- now.md ---' line."""
        out, remember = self._run(tmp_path, now_filler=12000)
        self._assert_handoff_intact(out)
        self._assert_listing_matches_reality(out, remember)
        assert _marker("now.md") not in out
        assert str(remember / "now.md") in _listed(out)


class TestMemoryContentCannotSteerTheCut:
    """The same threat one section later: a memory file is content too, and
    an earlier file holding a later file's header line must not move the
    cut."""

    def test_fake_archive_header_inside_now_md(self, tmp_path):
        bodies = _bodies(400)
        bodies["now.md"] = _marker("now.md") + "\n--- archive.md ---\nNOW-AFTER-FAKE-845\n"
        bodies["archive.md"] = _marker("archive.md") + "\n" + "a" * 9000 + "\n"
        env, remember = _store(tmp_path, bodies)
        out, _ = _start(env)
        assert "NOW-AFTER-FAKE-845" in out, "the cut landed inside now.md"
        assert _marker("archive.md") not in out
        assert _listed(out) == [str(remember / "archive.md")]
