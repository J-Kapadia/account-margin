"""The sign-off gate: `close` refuses to run an allocation-dependent step without a sign-off that
binds the current configs, exits 4, reports `CTL-SIGNOFF` as failed and writes nothing.

Tests sign temporary copies with `signoff.write_for`; the repository's own sign-offs are never written.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil

import pytest

from conftest import FIXTURE_SIGNOFF, MINI_CONFIG, close_mini
from margin import signoff
from margin.errors import MarginError


def _message(rule: str, signed_rule: str, policy: str, signed_policy: str) -> str:
    return (
        f"allocation rule not signed: rule expected {rule} signed {signed_rule}; "
        f"policy expected {policy} signed {signed_policy}. No allocation-dependent step ran."
    )


def _copy_config(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(MINI_CONFIG, config)
    return config


def _edit_policy(config, new_floor: int) -> None:
    policy = config / "policy.yaml"
    policy.write_text(re.sub(r"floor_pct: \d+", f"floor_pct: {new_floor}", policy.read_text()))


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_committed_fixture_signoff_passes_the_gate(monkeypatch):
    monkeypatch.setenv("MARGIN_SIGNOFF_DIR", str(FIXTURE_SIGNOFF))
    signed = signoff.verify(MINI_CONFIG)
    (committed,) = FIXTURE_SIGNOFF.glob("*.json")
    assert committed.stem == signed.ref == signoff.combined_sha256(*signoff.config_hashes(MINI_CONFIG))
    assert signed.signer == json.loads(committed.read_text())["signer"]


def test_no_sign_off_file_exits_4_with_the_scenario_message_and_writes_nothing(tmp_path):
    empty, warehouse = tmp_path / "empty", tmp_path / "w.duckdb"
    empty.mkdir()
    result = close_mini(warehouse, "2026-03", env={"MARGIN_SIGNOFF_DIR": str(empty)})
    rule, policy = signoff.config_hashes(MINI_CONFIG)
    assert result.code == 4
    env = result.json
    assert env["message"] == _message(rule, "none", policy, "none")
    assert env["controls"] == [{"id": "CTL-SIGNOFF", "status": "fail", "violating_rows": 1}]
    assert [(c["id"], c["violating_rows"]) for c in env["failed_controls"]] == [("CTL-SIGNOFF", 1)]
    assert not warehouse.exists()


def test_a_missing_sign_off_directory_is_the_same_failure(tmp_path):
    result = close_mini(tmp_path / "w.duckdb", "2026-03", env={"MARGIN_SIGNOFF_DIR": str(tmp_path / "absent")})
    assert result.code == 4 and "signed none" in result.json["message"]


def test_a_config_edited_after_signing_names_what_was_signed(tmp_path):
    config, signed_dir = _copy_config(tmp_path), tmp_path / "so"
    signoff.write_for(config, signer="s", role="r", signoff_to=signed_dir, signed_on="2026-10-01")
    rule, old_policy = signoff.config_hashes(config)
    _edit_policy(config, 31)
    new_policy = signoff.config_hashes(config)[1]
    result = close_mini(tmp_path / "w.duckdb", "2026-03", env={"MARGIN_SIGNOFF_DIR": str(signed_dir)}, config=config)
    assert result.code == 4
    assert result.json["message"] == _message(rule, rule, new_policy, old_policy)
    assert not (tmp_path / "w.duckdb").exists()


def test_with_two_historical_sign_offs_the_message_prints_the_newer_one(tmp_path):
    config, signed_dir = _copy_config(tmp_path), tmp_path / "so"
    signoff.write_for(config, signer="s", role="r", signoff_to=signed_dir, signed_on="2026-09-01")
    _edit_policy(config, 31)
    signoff.write_for(config, signer="s", role="r", signoff_to=signed_dir, signed_on="2026-10-01")
    newer = signoff.config_hashes(config)
    _edit_policy(config, 32)
    now = signoff.config_hashes(config)
    result = close_mini(tmp_path / "w.duckdb", "2026-03", env={"MARGIN_SIGNOFF_DIR": str(signed_dir)}, config=config)
    assert result.code == 4
    assert result.json["message"] == _message(now[0], newer[0], now[1], newer[1])


def test_the_newest_sign_off_is_chosen_by_date_then_by_file_name(tmp_path):
    def write(name, signed_on, tag):
        record = {"rule_sha256": tag, "policy_sha256": tag, "signer": "s", "role": "r", "signed_on": signed_on}
        (tmp_path / name).write_text(json.dumps(record))

    write("zzz.json", "2026-09-01", "older")
    write("aaa.json", "2026-10-01", "newer")
    assert signoff.most_recent(tmp_path)["rule_sha256"] == "newer"
    write("bbb.json", "2026-10-01", "same-day-later-name")
    assert signoff.most_recent(tmp_path)["rule_sha256"] == "same-day-later-name"
    (tmp_path / "ccc.json").write_text("not json")  # unreadable files are ignored
    assert signoff.most_recent(tmp_path)["rule_sha256"] == "same-day-later-name"
    assert signoff.most_recent(tmp_path / "absent") is None


def test_a_file_with_the_right_name_but_other_hashes_does_not_pass(tmp_path, monkeypatch):
    signed_dir = tmp_path / "so"
    signed_dir.mkdir()
    rule, policy = signoff.config_hashes(MINI_CONFIG)
    record = {"rule_sha256": rule, "policy_sha256": "0" * 64, "signer": "s", "role": "r", "signed_on": "2026-10-01"}
    (signed_dir / f"{signoff.combined_sha256(rule, policy)}.json").write_text(json.dumps(record))
    monkeypatch.setenv("MARGIN_SIGNOFF_DIR", str(signed_dir))
    with pytest.raises(MarginError) as raised:
        signoff.verify(MINI_CONFIG)
    assert raised.value.exit_code == 4


def test_a_failed_gate_leaves_a_closed_warehouse_byte_identical(mini_closed_copy, tmp_path):
    config = _copy_config(tmp_path)
    _edit_policy(config, 25)  # edited after the fixture was signed: no file binds it
    before = _sha(mini_closed_copy)
    result = close_mini(mini_closed_copy, "2026-03", config=config)
    assert result.code == 4
    assert result.json["failed_controls"][0]["id"] == "CTL-SIGNOFF"
    assert _sha(mini_closed_copy) == before
