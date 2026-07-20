import json

import pytest

from eval_harness.jsonutil import extract_json


def test_bare_json_happy_path():
    assert extract_json('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}


def test_markdown_fenced_with_json_tag():
    raw = '```json\n{"a": 1}\n```'
    assert extract_json(raw) == {"a": 1}


def test_markdown_fenced_without_json_tag():
    raw = '```\n{"a": 1}\n```'
    assert extract_json(raw) == {"a": 1}


def test_leading_and_trailing_prose():
    raw = 'Sure, here is the JSON you asked for:\n{"severity": "high"}\nLet me know if you need anything else.'
    assert extract_json(raw) == {"severity": "high"}


def test_malformed_json_raises_json_decode_error():
    with pytest.raises(json.JSONDecodeError):
        extract_json("{not: valid, json,}")


def test_missing_json_object_raises_json_decode_error():
    with pytest.raises(json.JSONDecodeError):
        extract_json("no JSON here at all")


def test_nested_braces_in_string_values_not_truncated():
    raw = '{"reasoning": "config has a {nested} placeholder and {another} one", "severity": "low"}'
    parsed = extract_json(raw)
    assert parsed == {
        "reasoning": "config has a {nested} placeholder and {another} one",
        "severity": "low",
    }


def test_greedy_match_grabs_outermost_braces_across_prose_json_prose():
    # Regex is greedy DOTALL, so it grabs from the first `{` to the *last* `}`
    # in the string. As long as there's only one JSON object with no stray
    # braces after it, this still parses correctly - documenting that behavior.
    raw = 'prefix text {"a": 1, "nested": {"b": 2}} suffix text'
    assert extract_json(raw) == {"a": 1, "nested": {"b": 2}}
