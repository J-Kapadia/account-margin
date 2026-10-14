"""`margin signoff` and `signoff.write_for`.

Tests sign temporary copies of a config directory; no sign-off is ever written under
`tests/fixtures/signoff/` or `config/signoff/` by a test: the author signs those in a terminal.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import shutil
import subprocess
import sys

import pytest

from conftest import MINI_CONFIG, ROOT, margin
from margin import signoff


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_for_names_the_file_by_the_combined_hash(tmp_path):
    out = tmp_path / "signoff"
    path = signoff.write_for(MINI_CONFIG, signer="Acme finance owner (synthetic)", role="finance_owner",
                             signoff_to=out, signed_on="2026-10-13")
    rule, policy = _sha(MINI_CONFIG / "allocation.yaml"), _sha(MINI_CONFIG / "policy.yaml")
    combined = hashlib.sha256(f"{rule}\n{policy}".encode()).hexdigest()
    assert path == out / f"{combined}.json"
    assert json.loads(path.read_text()) == {
        "rule_sha256": rule, "policy_sha256": policy, "signer": "Acme finance owner (synthetic)",
        "role": "finance_owner", "signed_on": "2026-10-13",
    }
    assert [p.name for p in out.iterdir()] == [path.name]


def test_the_directory_follows_the_environment_then_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("MARGIN_SIGNOFF_DIR", raising=False)
    assert signoff.signoff_dir(tmp_path) == tmp_path / "signoff"
    monkeypatch.setenv("MARGIN_SIGNOFF_DIR", str(tmp_path / "elsewhere"))
    assert signoff.signoff_dir(tmp_path) == tmp_path / "elsewhere"


def test_a_config_edit_after_signing_is_a_hash_mismatch(tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(MINI_CONFIG, cfg)
    first = signoff.write_for(cfg, signer="s", role="r", signoff_to=tmp_path / "so")
    policy = cfg / "policy.yaml"
    policy.write_text(policy.read_text().replace("floor_pct: 30", "floor_pct: 31"))
    second = signoff.write_for(cfg, signer="s", role="r", signoff_to=tmp_path / "so")
    assert first.name != second.name  # the in-force file is the one named by the current configs
    allocation = cfg / "allocation.yaml"
    allocation.write_text(allocation.read_text() + "\n")
    assert signoff.write_for(cfg, signer="s", role="r", signoff_to=tmp_path / "so").name not in (first.name, second.name)


def test_signing_needs_a_signer_and_a_role_and_both_configs(tmp_path):
    with pytest.raises(Exception, match="signer and a role"):
        signoff.write_for(MINI_CONFIG, signer=" ", role="r", signoff_to=tmp_path)
    (tmp_path / "allocation.yaml").write_text("x")
    with pytest.raises(Exception, match="policy file not found"):
        signoff.write_for(tmp_path, signer="s", role="r", signoff_to=tmp_path)


def test_editing_the_real_config_does_not_change_the_fixture_hash(tmp_path):
    before = signoff.config_hashes(MINI_CONFIG)
    real = tmp_path / "config"
    shutil.copytree(ROOT / "config", real)
    (real / "policy.yaml").write_text((real / "policy.yaml").read_text().replace("floor_pct: 30", "floor_pct: 25"))
    assert signoff.config_hashes(real) != signoff.config_hashes(ROOT / "config")
    assert signoff.config_hashes(MINI_CONFIG) == before  # the fixture reads its own directory
    assert signoff.config_hashes(MINI_CONFIG) != signoff.config_hashes(ROOT / "config")


def test_the_command_refuses_without_a_terminal(tmp_path):
    result = margin("signoff", "--signer", "me", "--role", "finance_owner", "--config", str(MINI_CONFIG), "--json",
                    env={"MARGIN_SIGNOFF_DIR": str(tmp_path / "so")})
    assert result.code == 4
    assert "terminal" in result.json["message"]
    assert not (tmp_path / "so").exists()


def test_the_command_writes_the_file_at_a_terminal(tmp_path):
    # A person at a terminal can sign; the test stands in for one with a pseudo-terminal, aimed at a
    # temporary directory, so nothing is signed for the repository.
    leader, follower = pty.openpty()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MARGIN_")}
    env["MARGIN_SIGNOFF_DIR"] = str(tmp_path / "so")
    proc = subprocess.run(
        [sys.executable, "-m", "margin", "signoff", "--signer", "me", "--role", "finance_owner",
         "--config", str(MINI_CONFIG)],
        cwd=ROOT, env=env, stdin=follower, stdout=follower, stderr=subprocess.PIPE, check=False,
    )
    os.close(leader), os.close(follower)
    assert proc.returncode == 0, proc.stderr
    (written,) = (tmp_path / "so").iterdir()
    assert json.loads(written.read_text())["role"] == "finance_owner"


def test_help_works():
    assert margin("signoff", "--help").code == 0


def test_signing_the_fixture_config_reproduces_the_committed_signoff_and_the_real_config_is_independent(tmp_path):
    committed = ROOT / "tests" / "fixtures" / "signoff"
    cfg = tmp_path / "config"
    shutil.copytree(MINI_CONFIG, cfg)
    written = signoff.write_for(cfg, signer="s", role="r", signoff_to=tmp_path / "so", signed_on="2026-10-14")
    assert (committed / written.name).is_file()  # the committed sign-off is named by the fixture config's hash
    assert json.loads(written.read_text())["rule_sha256"] == json.loads((committed / written.name).read_text())["rule_sha256"]
    real = tmp_path / "real"
    shutil.copytree(ROOT / "config", real)
    (real / "policy.yaml").write_text((real / "policy.yaml").read_text() + "\n# edited\n")
    assert signoff.write_for(MINI_CONFIG, signer="s", role="r", signoff_to=tmp_path / "again").name == written.name
