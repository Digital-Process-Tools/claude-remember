"""What `user-prompt-hook.sh` injects must be the user's choice (#301).

The hook prints `[HH:MM TZ — user — 45%]` into the model's context on every
prompt. Two of those three fields change between turns for reasons that have
nothing to do with what the user typed: the clock, and — for everyone running
the status line, which is the common case — the context percentage. A gateway
operator who wants that line to hold still has, today, exactly one option:
rewrite this hook's stdout in flight with a regex. A hook whose output has to be
patched by its consumers is a hook missing an option.

What is pinned here:

* `prompt_stamp` defaults to `full` and `full` is byte-for-byte what shipped
  before, because a caching preference must not silently take the clock away
  from installs that never asked;
* `stable` drops *both* volatile fields — not just the clock. Dropping the clock
  alone leaves `— 45%` climbing every single turn, which is the same defect
  under a shorter name;
* `stable` keeps the `>= 95` warning, which is threshold-gated: it changes bytes
  only when it changes behaviour, and it is the one line in this hook anybody
  acts on;
* `off` means off — including that warning. A user who asks for silence and gets
  a surprise line at 95% is a user back to writing the regex;
* none of it costs a process. This hook runs on every prompt and #227 is the
  scar: 27 spawns to print one line, p50 8718 ms on the reporter's box.

Deliberately NOT asserted here: any claim about prompt-prefix cache behaviour.
The issue reports a measurement on one self-hosted gateway; nothing in this repo
can verify it, and a test that encoded it would be pinning someone else's
infrastructure. The property tested is the one that is ours: the hook emits what
it was configured to emit, and holds still when asked to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

from tests.spawn_counting import make_shim_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_PROMPT = REPO_ROOT / "scripts" / "user-prompt-hook.sh"
README = REPO_ROOT / "README.md"

WHO = "cachetester"

# Same budget as tests/test_prompt_hook_spawns.py, imported by value rather than
# by name on purpose: this file asserts the *new* modes do not exceed what the
# old one already costs, so if that budget is ever raised for an unrelated
# reason, this file should fail and be looked at rather than follow it up.
WARM_SPAWN_BUDGET = 2


def _project(tmp_path: Path, config: dict | None = None):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".remember" / "tmp").mkdir(parents=True)
    (home / ".remember").mkdir(parents=True)
    if config is not None:
        cfg = home / ".remember" / "config.json"
        cfg.write_text(json.dumps(config), encoding="utf-8")
        # Backdated, and not for convenience. lib-env-cache.sh refuses a cache
        # that is not `-nt` every config layer, and bash's `-nt` compares whole
        # SECONDS — so a config written in the same second as the cache is
        # rejected and the next prompt re-resolves from scratch. That direction
        # fails safe (a stale answer is never served; the cost is one slow
        # prompt), but it makes any spawn count taken here a coin flip. A config
        # file the user edited before this session started is also what the
        # measurement is supposed to be about.
        old = time.time() - 60
        os.utime(cfg, (old, old))
    return home, project


def _env(home: Path, project: Path, extra: dict | None = None) -> dict:
    env = {
        **os.environ,
        "HOME": str(home),
        "USER": WHO,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "TMPDIR": str(project.parent / "tmp"),
    }
    (project.parent / "tmp").mkdir(exist_ok=True)
    for stale in ("REMEMBER_DIR", "_LIB_MEMORY_DIR_LOADED", "REMEMBER_TZ",
                  "REMEMBER_PROMPT_STAMP", "USERNAME"):
        env.pop(stale, None)
    if extra:
        env.update(extra)
    return env


def _run(env: dict, *, count_into: Path | None = None, shims: Path | None = None):
    if count_into is not None:
        count_into.write_text("", encoding="utf-8")
        env = {**env, "SPAWN_LOG": str(count_into),
               "PATH": f"{shims}{os.pathsep}{env['PATH']}"}
    return subprocess.run(
        ["bash", str(USER_PROMPT)],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _ctx(env: dict, pct: str) -> None:
    Path(env["TMPDIR"], "claude-ctx-pct").write_text(pct, encoding="utf-8")


def _stamp(tmp_path: Path, mode: str | None, pct: str | None = None) -> str:
    """Run the hook in `mode` and return its stdout."""
    config = {"timezone": "UTC"} if mode is None else {"timezone": "UTC", "prompt_stamp": mode}
    home, project = _project(tmp_path, config)
    env = _env(home, project)
    if pct is not None:
        _ctx(env, pct)
    result = _run(env)
    assert result.returncode == 0, (
        f"rc={result.returncode} — this hook must always exit 0, a non-zero exit "
        f"on UserPromptSubmit erases what the user typed. stderr={result.stderr[:400]}"
    )
    return result.stdout


# ── The default must not move ────────────────────────────────────────────────

def test_the_default_is_byte_for_byte_what_shipped_before(tmp_path):
    """No config, no change. This is the assertion the whole issue turns on:
    an option added for one gateway operator must not quietly restyle the line
    every other install has been reading for a year."""
    out = _stamp(tmp_path, None, pct="45").strip()
    assert out == f"[{_clock(out)} UTC — {WHO} — 45%]", out


def test_full_is_the_default_spelled_out(tmp_path):
    """Setting the documented default explicitly must be a no-op, not a variant."""
    out = _stamp(tmp_path, "full", pct="45").strip()
    assert out == f"[{_clock(out)} UTC — {WHO} — 45%]", out


def test_an_unrecognised_value_falls_back_to_full(tmp_path):
    """A typo in config.json must not silently delete the timestamp — the safe
    direction for an unknown value is the behaviour that already shipped."""
    out = _stamp(tmp_path, "stabel", pct="45").strip()
    assert out == f"[{_clock(out)} UTC — {WHO} — 45%]", (
        f"`prompt_stamp: stabel` was honoured as something. Got: {out!r}"
    )


def _clock(line: str) -> str:
    """The HH:MM the hook printed, so equality tests can assert everything else.

    Reconstructing the expected line rather than regex-matching it is the point:
    a regex with `.*` in it passes for output nobody intended.
    """
    assert line.startswith("["), line
    head = line[1:].split(" ", 1)[0]
    assert ":" in head, f"no clock at the head of {line!r}"
    return head


# ── stable: both volatile fields go, the warning stays ───────────────────────

def test_stable_emits_only_the_user(tmp_path):
    assert _stamp(tmp_path, "stable").strip() == f"[{WHO}]"


def test_stable_drops_the_context_percentage_too(tmp_path):
    """The half of the request the issue got wrong, and the half that matters.

    The reporter proposed keeping `[user]` plus the percentage, calling both
    byte-stable between turns. The username is. The percentage climbs every
    turn — so an option that removed only the clock would leave the line volatile
    for every user running the status line, which is most of them.
    """
    out = _stamp(tmp_path, "stable", pct="45").strip()
    assert out == f"[{WHO}]", (
        f"`stable` still carries the per-turn context percentage: {out!r}"
    )


def test_stable_is_identical_across_different_context_percentages(tmp_path):
    """The property, stated as a property rather than as an absence."""
    first = _stamp(tmp_path / "a", "stable", pct="12")
    second = _stamp(tmp_path / "b", "stable", pct="88")
    assert first == second, (
        f"`stable` is not stable: {first!r} at 12% vs {second!r} at 88%"
    )


def test_stable_still_warns_at_the_context_limit(tmp_path):
    """Threshold-gated, so it changes bytes only when it changes behaviour —
    and it is the one line in this hook a user acts on."""
    out = _stamp(tmp_path, "stable", pct="96")
    assert f"[{WHO}]" in out, out
    assert "96%" in out and "/remember" in out, (
        f"`stable` swallowed the context-death warning: {out!r}"
    )


# ── off: off ─────────────────────────────────────────────────────────────────

def test_off_emits_nothing(tmp_path):
    assert _stamp(tmp_path, "off", pct="45").strip() == ""


def test_off_suppresses_the_context_warning_as_well(tmp_path):
    """Documented, deliberate, and the reason `stable` exists as a separate mode.

    `off` is chosen by someone who needs this hook to contribute no bytes. A
    surprise line at 95% would put them straight back to the regex that this
    option exists to retire — so the warning is `stable`'s to keep, not `off`'s.
    """
    out = _stamp(tmp_path, "off", pct="99")
    assert out.strip() == "", (
        f"`off` emitted something at 99%: {out!r}. If this line is worth keeping "
        f"in `off`, README must say so — silence that is not silent is a lie."
    )


def test_off_still_delivers_a_human_notice(tmp_path):
    """The toggle governs what goes into the MODEL's context.

    `systemMessage` is the only output a HUMAN sees, and the notices carried on
    it (#200, #253, #298) are findings the model cannot act on. Suppressing the
    stamp must not suppress those — that would trade one silent failure for
    three.
    """
    home, project = _project(tmp_path, {"timezone": "UTC", "prompt_stamp": "off"})
    env = _env(home, project)
    _run(env)

    notice = project / ".remember" / "tmp" / "capture-gap-notice"
    notice.write_text("previous session was not captured — run /doctor", encoding="utf-8")

    result = _run(env)
    assert result.returncode == 0, result.stderr[:400]
    payload = json.loads(result.stdout)
    assert payload["systemMessage"].startswith("previous session was not captured"), payload
    assert WHO not in payload["hookSpecificOutput"]["additionalContext"], (
        f"`off` leaked a stamp into the model context: {payload!r}"
    )
    assert not notice.exists(), "the notice must still be consumed on delivery"


# ── The costs this hook is not allowed to pay ────────────────────────────────

def test_reading_the_option_costs_no_process_on_a_warm_prompt(tmp_path):
    """#227, one layer down.

    The option is resolved through the same path REMEMBER_TZ takes: read once
    from the merged config table, replayed from the env cache afterwards. If it
    ever grows its own `jq` here, it is a fork on every prompt the user waits
    on — 150-800 ms each on the box #227 was measured on.
    """
    home, project = _project(tmp_path, {"timezone": "UTC", "prompt_stamp": "stable"})
    env = _env(home, project)
    log = tmp_path / "spawns.log"
    shims = make_shim_dir(tmp_path, log)

    first = _run(env, count_into=log, shims=shims)
    assert first.returncode == 0, f"cold run failed: {first.stderr[:400]}"
    cold = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]

    second = _run(env, count_into=log, shims=shims)
    assert second.returncode == 0, f"warm run failed: {second.stderr[:400]}"
    warm = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]

    assert len(warm) <= WARM_SPAWN_BUDGET, (
        f"{len(warm)} external spawns with prompt_stamp set (cold run: {len(cold)}). "
        f"Spawned: {sorted(set(warm))}"
    )


def test_changing_the_option_takes_effect_on_the_next_prompt(tmp_path):
    """The env cache replays a resolved answer, and this is now one of the
    answers it carries. A cached mode that outlives the config edit that changed
    it is the #227 fast path turning into a lie."""
    home, project = _project(tmp_path, {"timezone": "UTC"})
    env = _env(home, project)
    warm = _run(env)
    assert "UTC" in warm.stdout, f"precondition: {warm.stdout!r}"

    cfg = home / ".remember" / "config.json"
    cfg.write_text(json.dumps({"timezone": "UTC", "prompt_stamp": "stable"}), encoding="utf-8")
    now = time.time() + 5
    os.utime(cfg, (now, now))

    after = _run(env)
    assert after.stdout.strip() == f"[{WHO}]", (
        f"config.json now says stable and the hook still prints the old line: "
        f"{after.stdout!r}"
    )


def test_a_cache_written_before_this_option_existed_is_not_trusted(tmp_path):
    """An upgrade must not serve a resolution that predates the key.

    The cache is invalidated by config mtime, not by plugin version — so a file
    written by the previous release carries no answer for this option at all. It
    has to be refused rather than read as an empty value, or the first prompt
    after an upgrade answers from a resolution that never considered the key.
    """
    home, project = _project(tmp_path, {"timezone": "UTC", "prompt_stamp": "stable"})
    env = _env(home, project)
    _run(env)

    caches = list(Path(env["TMPDIR"]).glob("remember-env-*"))
    assert caches, "no published resolution to downgrade"
    for cache in caches:
        kept = [ln for ln in cache.read_text(encoding="utf-8").splitlines()
                if not ln.startswith("REMEMBER_PROMPT_STAMP=")]
        assert len(kept) < len(cache.read_text(encoding="utf-8").splitlines()), (
            "the cache carries no REMEMBER_PROMPT_STAMP line, so the fast path "
            "cannot be honouring the option — it is re-resolving every prompt, "
            "or ignoring the setting"
        )
        cache.write_text("\n".join(kept) + "\n", encoding="utf-8")

    result = _run(env)
    assert result.returncode == 0, result.stderr[:400]
    assert result.stdout.strip() == f"[{WHO}]", (
        f"a pre-upgrade cache was replayed and the configured mode lost: "
        f"{result.stdout!r}"
    )


# ── Documentation is part of the option ──────────────────────────────────────

def test_the_option_is_documented_with_all_three_values(tmp_path):
    """tests/test_config_contract.py already asserts the key appears in the
    README table and the example config. It cannot assert that the table says
    what the values DO — and an option whose modes are undiscoverable is one
    people will keep solving with a regex."""
    text = README.read_text(encoding="utf-8")
    for value in ("`full`", "`stable`", "`off`"):
        assert value in text, f"README documents no {value} mode for prompt_stamp"
