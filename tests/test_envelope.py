"""The --json envelope: every command's output validates against the schema."""

import json
from pathlib import Path

import jsonschema
import pytest

from conftest import margin

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "src/margin/schemas/envelope.schema.json").read_text()
)
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def validate(env: dict) -> dict:
    VALIDATOR.validate(env)
    return env


def test_gen_envelope(tmp_path):
    result = margin("gen", "--fixture", "small", "--out", str(tmp_path / "b"), "--json")
    assert result.code == 0
    assert validate(result.json)["status"] == "ok"


def test_close_envelope(closed_copy):
    env = validate(margin("close", "--month", "2026-03", "--json", *closed_copy.flags()).json)
    assert env["controls"] == [
        {"id": c, "status": "pass", "violating_rows": 0}
        for c in ("CTL-SIGNOFF", "CTL-ALLOC-CONSERVE", "CTL-CUTOFF", "CTL-PRICE-PIN", "CTL-REV-DUP", "CTL-SIGN",
                  "CTL-TIE-CLD", "CTL-TIE-INF", "CTL-TIE-REV", "CTL-TIE-SUP")
    ]
    assert len(env["result_hash"]) == 64


def test_explain_and_memo_envelopes(closed, tmp_path):
    env = validate(margin("explain", "rollup/2026-03/revenue", "--json", *closed.warehouse_flags()).json)
    assert env["number_id"] == "rollup/2026-03/revenue"
    env = validate(
        margin("memo", "--month", "2026-03", "--out", str(tmp_path), "--json", *closed.warehouse_flags()).json
    )
    assert env["files"]


@pytest.mark.parametrize(
    "args,code",
    [
        (["close", "--month", "2025-09"], 2),   # outside the window
        (["close", "--month", "2026-03", "--check"], 4),  # truth/ absent
        (["memo", "--month", "2026-02"], 4),    # not closed
        (["explain", "cp/2026-03/acct_9999/revenue"], 2),  # unknown id
        (["gen", "--prices", "/nonexistent/model_prices.csv"], 4),  # no price list
    ],
)
def test_failure_envelopes(closed_copy, args, code):
    flags = closed_copy.warehouse_flags() if args[0] in ("memo", "explain") else closed_copy.flags()
    result = margin(*args, "--json", *flags)
    assert result.code == code, result.stderr
    env = validate(result.json)
    assert env["status"] == "fail" and env["exit"] == code


@pytest.mark.parametrize(
    "args",
    [
        ["close", "--month", "bogus"],
        ["explain", "--depth", "-1", "x"],
        ["inject", "--error", "nope", "--out", "o"],
        ["close"],  # missing --month
    ],
)
def test_usage_error_prints_failure_envelope_under_json(args):
    result = margin(*args, "--json")
    assert result.code == 2, result.stderr
    env = validate(result.json)
    assert env["status"] == "fail" and env["exit"] == 2
    assert env["message"] and "Usage:" in result.stderr


def test_usage_error_without_json_prints_no_envelope():
    result = margin("close", "--month", "bogus")
    assert result.code == 2 and result.stdout == ""


def test_schema_rejects_malformed_envelopes():
    good = {"schema_version": 1, "status": "ok", "exit": 0}
    VALIDATOR.validate(good)
    bad = [
        {**good, "schema_version": 2},
        {**good, "exit": 3},                               # ok must exit 0
        {"schema_version": 1, "status": "fail", "exit": 3, "message": "x"},  # exit 3 needs failed_controls
        {"schema_version": 1, "status": "fail", "exit": 2},                  # a failure needs a message
        {**good, "month": "2026-13"},
        {**good, "controls": [{"id": "tie", "status": "pass", "violating_rows": 0}]},
        {**good, "controls": [{"id": "CTL-X", "status": "maybe", "violating_rows": 0}]},
    ]
    for env in bad:
        with pytest.raises(jsonschema.ValidationError):
            VALIDATOR.validate(env)
