"""PROOF (temporary) for #1014 round 2 -- removed in the finalizing commit
on this branch.

Shows that `faulthandler_exit_on_timeout` actually kills a genuinely hung
test PROCESS, converting a hang into a FAILED step with a traceback in a
kept log, rather than depending on the runner's own step/job timeout --
which this PR's own earlier CI run showed does not reliably fire on
Windows (run 38122057767, job 114418645594: cancelled, no log, after the
step ran ~33.6 min past its own 25min timeout).

This sleeps forever (never returns on its own), unlike the earlier,
already-landed #1014 proof's 8s sleep -- that one only proved the dump
fires; this one proves the PROCESS actually exits. pyproject.toml's
`faulthandler_timeout` is lowered to a small value for this one proof
commit so the CI run itself stays short.
"""

import time


def test_proof_genuine_hang_is_killed_by_faulthandler_exit_on_timeout():
    time.sleep(10**9)
