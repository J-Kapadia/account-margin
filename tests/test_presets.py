"""The allocation config truth table: presets, their drivers and the shared costs they cover."""

from __future__ import annotations

import shutil

import pytest
import yaml

from conftest import MINI_CONFIG
from margin import gen_config
from margin.errors import EXIT_USAGE, MarginError
from margin.gen_config import load_allocation

COSTS = gen_config.KNOWN_SHARED_COSTS


def _mapping(alloc, name):
    return dict(next(p for p in alloc.presets if p.name == name).drivers)


def _write(tmp_path, body: dict):
    cfg = tmp_path / f"config{len(list(tmp_path.iterdir()))}"
    shutil.copytree(MINI_CONFIG, cfg)
    (cfg / "allocation.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return cfg


def _four(**overrides):
    presets = {n: {"drivers": dict(m)} for n, m in gen_config.BUILTIN_PRESETS.items()}
    for name, mapping in overrides.items():
        presets[name] = {"drivers": mapping}
    return {"presets": presets, "allowed": list(presets), "recommended": "by_driver"}


def test_the_vocabulary_is_the_four_drivers_four_costs_and_four_presets():
    assert gen_config.KNOWN_DRIVERS == ("tokens", "revenue", "usage_accounts", "tickets")
    assert gen_config.KNOWN_SHARED_COSTS == (
        "cloud_untagged", "cloud_unused_commitment", "support_shared", "platform_fees")
    assert gen_config.KNOWN_RULES == ("by_tokens", "by_revenue", "equal_usage", "by_driver")


def test_a_file_without_presets_reads_as_the_three_built_in_presets():
    alloc = load_allocation(MINI_CONFIG)
    assert alloc.rules == ("by_tokens", "by_revenue", "equal_usage")
    assert [p.name for p in alloc.presets] == list(alloc.rules)
    assert alloc.recommended == "by_tokens"
    for name, driver in (("by_tokens", "tokens"), ("by_revenue", "revenue"), ("equal_usage", "usage_accounts")):
        assert _mapping(alloc, name) == {cost: driver for cost in COSTS}


def test_a_presets_file_with_the_four_loads_them_all(tmp_path):
    alloc = load_allocation(_write(tmp_path, _four()))
    assert alloc.rules == ("by_tokens", "by_revenue", "equal_usage", "by_driver")
    assert alloc.recommended == "by_driver"
    assert _mapping(alloc, "by_driver") == {
        "cloud_untagged": "tokens", "cloud_unused_commitment": "tokens",
        "support_shared": "tickets", "platform_fees": "revenue",
    }
    assert next(p for p in alloc.presets if p.name == "by_driver").driver_for("support_shared") == "tickets"


def test_by_driver_is_built_in_when_the_file_allows_it_without_defining_it(tmp_path):
    body = {"allowed": ["by_driver", "by_tokens"], "recommended": "by_driver"}
    alloc = load_allocation(_write(tmp_path, body))
    assert alloc.rules == ("by_driver", "by_tokens")
    assert _mapping(alloc, "by_driver")["platform_fees"] == "revenue"


def test_a_preset_may_override_one_mapping(tmp_path):
    mapping = dict(gen_config.BUILTIN_PRESETS["by_driver"], platform_fees="tokens")
    alloc = load_allocation(_write(tmp_path, _four(by_driver=mapping)))
    assert _mapping(alloc, "by_driver")["platform_fees"] == "tokens"
    assert _mapping(alloc, "by_driver")["support_shared"] == "tickets"
    assert _mapping(alloc, "by_tokens") == {cost: "tokens" for cost in COSTS}


def test_a_driver_outside_the_set_is_exit_2_naming_the_preset_and_the_driver(tmp_path):
    mapping = dict(gen_config.BUILTIN_PRESETS["by_driver"], support_shared="headcount")
    with pytest.raises(MarginError) as exc:
        load_allocation(_write(tmp_path, _four(by_driver=mapping)))
    assert exc.value.exit_code == EXIT_USAGE
    assert "by_driver" in str(exc.value) and "headcount" in str(exc.value)


def test_a_missing_shared_cost_is_exit_2_naming_the_preset_and_the_cost(tmp_path):
    mapping = {c: d for c, d in gen_config.BUILTIN_PRESETS["by_driver"].items() if c != "support_shared"}
    with pytest.raises(MarginError) as exc:
        load_allocation(_write(tmp_path, _four(by_driver=mapping)))
    assert exc.value.exit_code == EXIT_USAGE
    assert "by_driver" in str(exc.value) and "support_shared" in str(exc.value)


def test_a_shared_cost_outside_the_set_is_exit_2(tmp_path):
    mapping = dict(gen_config.BUILTIN_PRESETS["by_driver"], payroll="tokens")
    with pytest.raises(MarginError) as exc:
        load_allocation(_write(tmp_path, _four(by_driver=mapping)))
    assert exc.value.exit_code == EXIT_USAGE and "payroll" in str(exc.value)


def test_an_unknown_preset_name_or_recommendation_is_exit_2(tmp_path):
    body = _four()
    body["allowed"] = ["by_tokens", "by_magic"]
    with pytest.raises(MarginError) as exc:
        load_allocation(_write(tmp_path, body))
    assert exc.value.exit_code == EXIT_USAGE and "by_magic" in str(exc.value)
    body = _four()
    body["recommended"] = "by_magic"
    with pytest.raises(MarginError) as exc:
        load_allocation(_write(tmp_path, body))
    assert exc.value.exit_code == EXIT_USAGE
