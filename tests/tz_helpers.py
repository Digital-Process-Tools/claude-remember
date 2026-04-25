"""Shared test fixtures for the memory pipeline test suite."""

import os
import sys
from datetime import datetime

import pytest

# Ensure pipeline package is importable from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FrozenDatetime:
    """A minimal drop-in for ``datetime`` whose ``now()`` returns a fixed moment.

    Used to patch ``pipeline._tz.datetime`` so that date/time helpers produce
    deterministic results regardless of when or where the test runs.

    Usage::

        moment = datetime(2026, 4, 23, 3, 12, 0, tzinfo=timezone.utc)
        with patch("pipeline._tz.datetime", frozen_datetime(moment)):
            assert _tz.today_str() == "2026-04-22"  # EDT
    """

    def __init__(self, moment_utc: datetime):
        self._moment = moment_utc

    def now(self, tz=None):
        if tz is None:
            return self._moment.astimezone().replace(tzinfo=None)
        return self._moment.astimezone(tz)


def frozen_datetime(moment_utc: datetime) -> FrozenDatetime:
    """Return a frozen datetime replacement anchored at *moment_utc*."""
    return FrozenDatetime(moment_utc)


@pytest.fixture
def frozen_clock():
    """Fixture that returns the ``frozen_datetime`` factory for use in tests."""
    return frozen_datetime
