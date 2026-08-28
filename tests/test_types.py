"""Tests for pipeline data structures."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.types import TokenUsage, HaikuResult, ExtractResult, SaveResult, ConsolidationResult


def test_token_usage_defaults():
    t = TokenUsage()
    assert t.input == 0
    assert t.output == 0
    assert t.cache == 0
    assert t.cost_usd == 0.0


def test_token_usage_str():
    t = TokenUsage(input=1000, output=200, cache=500, cost_usd=0.0012)
    assert str(t) == "1000+500cache->200out ($0.0012)"


def test_haiku_result_defaults():
    r = HaikuResult()
    assert r.text == ""
    assert r.is_skip is False
    assert r.tokens.input == 0


def test_extract_result_defaults():
    e = ExtractResult()
    assert e.exchanges == ""
    assert e.position == 0
    assert e.human_count == 0
    assert e.corrupt_lines == 0


def test_save_result_defaults():
    s = SaveResult()
    assert s.action == ""
    assert s.entry == ""
    assert s.position == 0


def test_consolidation_result_defaults():
    c = ConsolidationResult()
    assert c.recent == ""
    assert c.archive == ""
