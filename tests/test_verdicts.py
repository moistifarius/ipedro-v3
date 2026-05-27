"""Tests for the tiny ACCEPT/REFUSE / PASS/FAIL line parser."""

from __future__ import annotations

from ipedro.duckhunt.verdicts import parse_verdict


def test_positive_verdict():
    v, body = parse_verdict("ACCEPT: sure, fine.", "ACCEPT", "REFUSE")
    assert v is True
    assert body == "sure, fine."


def test_negative_verdict():
    v, body = parse_verdict("REFUSE: not today.", "ACCEPT", "REFUSE")
    assert v is False
    assert body == "not today."


def test_case_insensitive_token():
    v, body = parse_verdict("accept: lowercase token", "ACCEPT", "REFUSE")
    assert v is True
    assert body == "lowercase token"


def test_strips_surrounding_quotes():
    v, body = parse_verdict('ACCEPT: "quoted"', "ACCEPT", "REFUSE")
    assert v is True
    assert body == "quoted"


def test_picks_first_nonempty_line():
    v, body = parse_verdict("\n\nREFUSE: nope.\nignored second line", "ACCEPT", "REFUSE")
    assert v is False
    assert body == "nope."


def test_unknown_format_returns_none_with_raw_body():
    v, body = parse_verdict("hmm, neither", "ACCEPT", "REFUSE")
    assert v is None
    assert "hmm" in body


def test_none_input_safe():
    assert parse_verdict(None, "ACCEPT", "REFUSE") == (None, "")
    assert parse_verdict("", "ACCEPT", "REFUSE") == (None, "")


def test_works_for_pass_fail_judge():
    v, body = parse_verdict("PASS: nailed it", "PASS", "FAIL")
    assert v is True and body == "nailed it"
    v, body = parse_verdict("FAIL: try again", "PASS", "FAIL")
    assert v is False and body == "try again"
