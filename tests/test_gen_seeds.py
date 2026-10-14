"""The full books generate for any seed, with every planted account inside its band.

A planted account's margin is tuned by moving one free cost, so a plan where the platform fee
alone keeps an account's margin above its band makes `gen` fail for that seed only. This sweeps
a range of seeds, including ones that once failed, and re-derives every account's flag from
the written books rather than trusting the generator's own check.
"""

from __future__ import annotations

import json

import pytest

from test_gen import CONFIG, check_bands, derive, run_gen
from margin import gen_config

SEEDS = [*range(1, 14), 99]

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("seed", SEEDS)
def test_gen_succeeds_with_every_planted_account_inside_its_band(seed, tmp_path):
    result = run_gen(tmp_path, "--seed", str(seed))
    assert result.code == 0, result.stderr
    policy = gen_config.load_config(CONFIG).policy
    planted = json.loads((tmp_path / "truth" / "planted_flags.json").read_text())
    check_bands(derive(tmp_path / "books"), planted, policy)
