"""Throwaway proof for #1014 -- removed before the PR is finalized.

Sleeps past the temporarily-lowered `faulthandler_timeout` in
pyproject.toml so a CI run names this test and dumps its traceback. This
file and the lowered pyproject.toml value are both reverted to the real
settings once the proof is captured from a job log.
"""

import time


def test_proof_sleep_past_faulthandler_timeout():
    time.sleep(8)
