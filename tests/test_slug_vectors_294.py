"""`docs/slug-vectors.json` is the slug contract, and this file is what keeps it true (#294).

The reporter of #294 maintains a PowerShell port of `session_dir_slug`, because
driving `remember` from a non-bash caller otherwise means forking bash on every
tool call to ask what slug the plugin computed. That port is how the long-path
divergence was found — and it is also a second implementation of the one
function in this codebase whose disagreements are silent by construction: a slug
that misses names a directory that does not exist, so the pipeline finds no
transcript, exits 0, and saves nothing.

The answer is NOT a prose spec (it drifts from `scripts/lib-slug.sh` and still
reads as current — this repo has been bitten by exactly that) and NOT a
`remember slug <path>` subcommand (the same fork, with a nicer name). It is a
checked-in vector file that **our own suite asserts against the implementation**,
so a port that diverges from it diverges from something our CI is already
holding still, and a change on our side that would move an existing user's store
fails here first.

Three things are asserted, and they fail independently:

1. the checked-in bytes are exactly what the implementation produces today —
   the anti-drift guarantee, and the only one that makes the file a contract;
2. every vector reproduces under the shell implementation in the environment it
   names, which is what an external reader is actually promised; and
3. every environment-independent vector reproduces under `pipeline/slug.py`
   too, because the plugin runs both and a port cannot tell which one answered.

What this file does NOT do: prove the slugs are *right*. They are generated from
the implementation, so they can only ever pin agreement. Truth is pinned by
`tests/test_slug_parity.py`, which transcribes Claude Code's own `JXA` from the
shipped bundle and never imports ours.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.slug import session_dir_slug as py_slug  # noqa: E402

from .slug_vectors import (  # noqa: E402
    INPUTS,
    VECTOR_FILE,
    bash_slug,
    generate,
    real_cygpath_on_path,
    render,
)

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="bash subprocess assertions — not portable to Windows runners (#79)",
    ),
    pytest.mark.skipif(
        real_cygpath_on_path(),
        reason="a real cygpath is on PATH; the no-cygpath half of every vector needs it absent",
    ),
]


def _load() -> dict:
    if not VECTOR_FILE.exists():
        return {}
    return json.loads(VECTOR_FILE.read_text(encoding="utf-8"))


DOC = _load()
VECTORS: list[dict] = DOC.get("vectors", [])
IDS = [v["id"] for v in VECTORS]


# ── The file itself ──────────────────────────────────────────────────────────


def test_the_contract_file_is_present():
    """Everything below is vacuous without it, and pytest reports a
    zero-parameter parametrize as a pass. So this is stated once, plainly."""
    assert VECTOR_FILE.exists(), (
        f"{VECTOR_FILE} is the published slug contract and does not exist. "
        "Generate it with: python3 -m tests.slug_vectors"
    )
    assert VECTORS, "the contract file carries no vectors"


def test_the_file_says_what_it_is():
    """A generated file that does not announce it will be hand-edited."""
    comment = "\n".join(DOC.get("$comment", []))
    assert "GENERATED" in comment
    assert "python3 -m tests.slug_vectors" in comment
    assert DOC.get("implementation") == "scripts/lib-slug.sh"
    assert DOC.get("vector_count") == len(VECTORS)
    assert len(IDS) == len(set(IDS)), "vector ids must be unique — they are how a port names a case"


def test_the_checked_in_bytes_are_what_the_implementation_produces_today():
    """The whole guarantee, in one assertion.

    A hand-maintained table of expected values is a second implementation, and
    a second implementation is the thing this feature exists to delete. So the
    file is regenerated here, from the real `session_dir_slug`, and compared
    byte for byte. Any change to the slug that would move an existing store
    fails here — loudly, in our CI, before it reaches anyone's memory.
    """
    assert VECTOR_FILE.read_text(encoding="utf-8") == render(generate()), (
        "docs/slug-vectors.json no longer matches the implementation. If the "
        "slug changed deliberately, understand first that every path whose "
        "slug moved is a STORE RENAME for the users on it, then regenerate "
        "with: python3 -m tests.slug_vectors"
    )


# ── The vectors, one at a time ───────────────────────────────────────────────


@pytest.mark.parametrize("vector", VECTORS, ids=IDS)
def test_the_shell_implementation_reproduces_the_vector(vector):
    raw = base64.b64decode(vector["path_b64"])
    if vector["cygpath"] == "agnostic":
        assert bash_slug(raw, cygpath=True) == vector["slug"]
        assert bash_slug(raw, cygpath=False) == vector["slug"]
    else:
        assert bash_slug(raw, cygpath=vector["cygpath"] == "present") == vector["slug"]


@pytest.mark.parametrize(
    "vector",
    [v for v in VECTORS if v["cygpath"] != "present" and v["path"] is not None],
    ids=[v["id"] for v in VECTORS if v["cygpath"] != "present" and v["path"] is not None],
)
def test_the_python_implementation_reproduces_the_vector(vector):
    """The plugin runs two implementations and a caller cannot tell which
    answered — `pipeline/slug.py` for the transcript lookup, the shell one on
    every tool call. A vector that only one of them satisfies is not a contract.

    Restricted to the vectors that describe the pure function: `pipeline/slug.py`
    deliberately never runs `cygpath` (the prefix is ours, not Claude Code's —
    see #294), and the ill-formed inputs have no `str` form to hand it.
    """
    assert py_slug(vector["path"]) == vector["slug"]


# ── What the vectors must cover ──────────────────────────────────────────────

# Stated here rather than left implicit in the input list, because the point of
# a conformance file is that a port can trust the coverage. Every shape the
# suite already parametrizes elsewhere: the six from #263, #294's long paths,
# the truncation boundary, and the non-ASCII code-unit cases.
REQUIRED_SHAPES = {
    "posix-macos",
    "win-msys",
    "win-drive-lower-slash",
    "win-drive-upper-slash",
    "win-drive-lower-back",
    "win-drive-upper-back",
    "win-cygdrive",
    "unc-msys",
    "win-long-msys",
    "win-long-native",
    "unc-long-msys",
    "looks-long-path-prefixed",
    "truncate-boundary-199",
    "truncate-boundary-200",
    "truncate-boundary-201",
    "truncate-deep-project",
    "nonascii-latin",
    "nonascii-emoji",
    "nonascii-astral-kanji",
    "illformed-latin1",
}


def test_every_shape_the_suite_parametrizes_is_covered():
    base_ids = {i.split(".")[0] for i in IDS}
    assert REQUIRED_SHAPES <= base_ids, sorted(REQUIRED_SHAPES - base_ids)


def test_at_least_one_vector_exercises_the_truncation_hash():
    """Without one, a port can pass the whole file and still miss #157."""
    truncated = [v for v in VECTORS if v["truncated"]]
    assert truncated, "no vector crosses 200 characters"
    for v in truncated:
        head, _, tail = v["slug"].rpartition("-")
        assert len(head) == 200, v["id"]
        assert tail and all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in tail), v["id"]


# ── The environment a vector depends on ──────────────────────────────────────


def test_no_vector_claims_an_empty_slug():
    """An empty slug is not a miss — it resolves to `~/.claude/projects/`
    ITSELF, a directory that exists and holds every project's transcripts. A
    contract file that published one would be handing a port the plugin's
    nastiest failure mode as an expected value."""
    for v in VECTORS:
        assert v["slug"], v["id"]


def test_a_cygpath_dependent_vector_says_so_and_names_its_pair():
    """A port with no cygpath cannot use the file correctly unless every case
    that needs one is marked — and marked in a way that shows what the same
    input does WITHOUT it, rather than leaving the other half unstated."""
    by_id = {v["id"]: v for v in VECTORS}
    for v in VECTORS:
        assert v["cygpath"] in ("agnostic", "present", "absent"), v["id"]
        if v["cygpath"] == "present":
            assert "cygpath" in v["requires"], v["id"]
            assert v["id"].endswith(".cygpath"), v["id"]
            assert v["id"][: -len(".cygpath")] + ".no-cygpath" in by_id, v["id"]
        elif v["cygpath"] == "absent":
            assert "cygpath" not in v["requires"], v["id"]
            assert v["id"].endswith(".no-cygpath"), v["id"]
        else:
            assert v["requires"] == [] or "cygpath" not in v["requires"], v["id"]


def test_an_ill_formed_vector_names_what_it_needs():
    """The shell only consults the UTF-8 decoder where an ill-formed path can
    exist — on Linux always, elsewhere only under REMEMBER_UTF8_STRICT=1. So
    these are the one group in the file whose expected value is not what an
    ordinary macOS shell would produce, and a reader has to be told."""
    illformed = [v for v in VECTORS if v["path"] is None]
    assert illformed, "no ill-formed input is covered"
    for v in illformed:
        assert "utf8-decoder" in v["requires"], v["id"]
        assert "python3" in v["requires"] and "iconv" in v["requires"], v["id"]
    assert DOC.get("generated_with", {}).get("REMEMBER_UTF8_STRICT") == "1"


def test_every_input_produced_a_vector():
    """A generator that silently dropped an input would leave a shape the
    coverage test above cannot see, because it checks ids that exist."""
    base_ids = {i.split(".")[0] for i in IDS}
    assert base_ids == {spec["id"] for spec in INPUTS}
