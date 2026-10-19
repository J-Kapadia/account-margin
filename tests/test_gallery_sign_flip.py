"""The sign_flip case of the gallery: its prose, and a committed capture that is exactly what the
close says today when the error is planted in the `mini` books."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from margin.gallery import CAPTURES, sign_flip as case
from test_injected_errors import NAMED, Closes, expected_number_id

ERROR = "sign_flip"


@pytest.fixture(scope="module")
def closes(tmp_path_factory) -> Closes:
    return Closes(tmp_path_factory)


@pytest.fixture(scope="module")
def capture() -> dict:
    return json.loads((CAPTURES / f"{ERROR}.json").read_text(encoding="utf-8"))


def test_the_capture_has_the_fixed_keys_in_order(capture):
    assert list(capture) == ["error", "seed", "month", "books", "target", "close"]
    assert list(capture["close"]) == ["exit", "message", "controls", "failed_controls"]


def test_the_capture_is_what_the_close_says_now(capture, closes):
    live = closes[ERROR]
    envelope = live["result"].json

    assert capture["target"] == live["target"]
    assert capture["close"]["exit"] == 3 == live["result"].code
    assert capture["close"]["failed_controls"] == envelope["failed_controls"]
    assert capture["close"]["controls"] == envelope["controls"]
    assert capture["close"]["message"] == envelope["message"]


def test_the_named_control_failed_and_names_the_revenue_number(capture, closes):
    live = closes[ERROR]
    failed = {f["id"]: f for f in capture["close"]["failed_controls"]}

    assert case.CONTROL == NAMED[ERROR]
    assert case.CONTROL in failed
    assert expected_number_id(ERROR, live["target"]) in "\n".join(failed[case.CONTROL]["detail"])


def test_the_prose_is_present():
    assert case.ERROR == ERROR
    for text in (case.TITLE, case.PLANTED, case.CAUGHT):
        assert isinstance(text, str) and text.strip()
    assert isinstance(case.REMEDIATION, list) and case.REMEDIATION
    assert all(isinstance(step, str) and step.strip() for step in case.REMEDIATION)


def test_the_module_defines_no_function():
    tree = ast.parse(Path(case.__file__).read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
