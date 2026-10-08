"""#982: docs/configuration.md's `git_reconcile.enabled` row said the hook
"exits immediately, before even reading this flag" -- #979 made that false:
`hooks.d/after_save/60-git-reconcile.sh` lines ~111-115 read the flag FIRST,
and when it is `true` the hook appends a `disabled pending #969` notice to
`hook-errors.log` on every save (which trips `/remember:doctor`'s Recent
errors WARN for exactly the users who opted in). #979 fixed the identical
"exits before ever reading" wording in eight test copies but missed this
shipped doc line.

Would this test pass if nothing changed? No: the pre-fix row explicitly
contained the string "exits immediately, before even reading this flag",
confirmed by reading docs/configuration.md at the base commit (dc1e02e)
before this fix -- see the PR's own red-test transcript.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGURATION = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
RECONCILE_HOOK = (REPO_ROOT / "hooks.d" / "after_save" / "60-git-reconcile.sh").read_text(
    encoding="utf-8"
)


def test_reconcile_flag_doc_no_longer_claims_it_is_never_read():
    assert "exits immediately, before even reading this flag" not in CONFIGURATION
    assert "reads this flag first, then exits immediately either way" in CONFIGURATION


def test_reconcile_flag_doc_names_the_hook_errors_notice():
    """The doc row must now describe the actual read-then-notify behavior
    the code has -- not merely drop the false claim."""
    assert "hook-errors.log" in CONFIGURATION
    assert "/remember:doctor" in CONFIGURATION


def test_reconcile_hook_actually_reads_the_flag_before_exiting():
    """Positive control: pins that the code side of this claim is still
    true, so a future change to the hook that stopped reading the flag
    first would fail here rather than only in the doc-wording assertions
    above, which cannot see the code at all."""
    config_read = RECONCILE_HOOK.find('config ".git_reconcile.enabled"')
    exit_call = RECONCILE_HOOK.find("\nexit 0", config_read if config_read != -1 else 0)
    assert config_read != -1, "hook no longer reads git_reconcile.enabled at all"
    assert exit_call != -1 and exit_call > config_read, (
        "the flag read must come before the hook's own unconditional exit"
    )
