"""The generator, its answer key and the manifest that binds them to the config.

The full books are generated once per session from the fixture price list (never real prices).
Every check re-derives its answer from the written CSV files with the Python reference, so the
tests do not trust the generator's own bookkeeping.
"""

from __future__ import annotations

import csv
import json
import shutil
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from conftest import ROOT, margin, shared_dir
from margin import gen_config, gen_plan
from margin.errors import MarginError
from margin import truth_reference as ref

PRICES = ROOT / "tests" / "fixtures" / "model_prices.csv"
CONFIG = ROOT / "config"
RATES = ROOT / "data" / "reference" / "support_rates.csv"
MARCH, APRIL = "2026-03", "2026-04"


# ------------------------------------------------------------------ helpers


def run_gen(root: Path, *args: str, config: Path = CONFIG, prices: Path = PRICES, fixture: str | None = None):
    cmd = ["gen", "--json", "--books", str(root / "books"), "--truth", str(root / "truth"),
           "--config", str(config), "--prices", str(prices)]
    if fixture:
        cmd += ["--fixture", fixture]
    return margin(*cmd, *args)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def derive(books: Path, config: Path = CONFIG) -> dict[str, ref.MonthResult]:
    config_ = gen_config.load_config(config)
    drivers = [(d.code, d.median_floor) for d in config_.policy.drivers]
    return ref.recompute_from_books(
        books, PRICES, RATES, config_.allocation.rules, config_.policy.floor_pct, drivers
    )


def cents(amount: str) -> int:
    return int(Decimal(amount) * 100)


def points(c: ref.Components) -> list[Fraction]:
    return [ref.margin_points(p, c.revenue) for p in c.profit.values()]


@pytest.fixture(scope="session")
def full(tmp_path_factory) -> Path:
    def build(root: Path) -> None:
        result = run_gen(root)
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "gen-full", build)


@pytest.fixture(scope="session")
def mini(tmp_path_factory) -> Path:
    def build(root: Path) -> None:
        result = run_gen(root, fixture="mini")
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "gen-mini", build)


@pytest.fixture(scope="session")
def scenario(tmp_path_factory) -> Path:
    def build(root: Path) -> None:
        result = run_gen(root, fixture="scenario")
        assert result.code == 0, result.stderr

    return shared_dir(tmp_path_factory, "gen-scenario", build)


@pytest.fixture(scope="session")
def policy() -> gen_config.Policy:
    return gen_config.load_config(CONFIG).policy


# ------------------------------------------------------------------ exit codes and inputs


def test_gen_exits_4_naming_prices_without_a_price_list(tmp_path):
    result = run_gen(tmp_path, prices=tmp_path / "model_prices.csv")
    assert result.code == 4
    assert "--prices" in result.json["message"]
    assert not (tmp_path / "books").exists()


def test_bare_gen_without_the_authors_prices_exits_4_naming_prices():
    if (ROOT / "data" / "reference" / "model_prices.csv").exists():
        pytest.skip("the author's price list exists: a bare gen would write the real books")
    result = margin("gen", "--json")
    assert result.code == 4 and "--prices" in result.json["message"]


def test_gen_exits_2_without_a_policy_file(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(CONFIG, config)
    (config / "policy.yaml").unlink()
    result = run_gen(tmp_path, config=config)
    assert result.code == 2
    assert "policy" in result.json["message"]
    assert not (tmp_path / "books").exists()


def test_gen_exits_2_for_an_unusable_config(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(CONFIG, config)
    (config / "allocation.yaml").write_text("recommended: by_tokens\nrules:\n  by_magic: {}\n")
    assert run_gen(tmp_path, config=config).code == 2
    shutil.copy(CONFIG / "allocation.yaml", config / "allocation.yaml")
    text = (config / "policy.yaml").read_text().replace("DEEP_DISCOUNT", "DEEP_DISCOUNTS")
    (config / "policy.yaml").write_text(text)
    assert run_gen(tmp_path, config=config).code == 2


def test_a_preset_fixes_the_account_count_and_window(tmp_path):
    assert run_gen(tmp_path, "--accounts", "5", fixture="mini").code == 2
    assert run_gen(tmp_path, "--first-month", "2026-01", fixture="scenario").code == 2


def test_prices_must_be_whole_nano_usd_and_sourced(tmp_path):
    bad = tmp_path / "prices.csv"
    bad.write_text("provider,model,input_per_mtok,output_per_mtok,source_url,retrieved_on\n"
                   "p,m,1.2345,2.00,fixture://x,2026-10-13\n")
    assert run_gen(tmp_path, prices=bad).code == 2
    bad.write_text("provider,model,input_per_mtok,output_per_mtok,source_url,retrieved_on\n"
                   "p,m,1.00,2.00,,2026-10-13\n")
    assert run_gen(tmp_path, prices=bad).code == 2


def test_the_fixture_price_list_is_marked_as_a_fixture():
    rows = read_csv(PRICES)
    assert len(rows) >= 3
    assert all(r["source_url"].startswith("fixture://") and r["provider"].startswith("fixture-") for r in rows)


def test_support_rates_file_matches_the_generators_ledger_table():
    assert ref.read_rates(RATES) == gen_plan.SUPPORT_RATES
    assert list(read_csv(RATES)[0]) == ["tier", "cents_per_minute", "basis"]
    assert {r["basis"] for r in read_csv(RATES)} == {"synthetic policy"}


# ------------------------------------------------------------------ determinism, window, manifest


def test_generation_is_deterministic_and_seeded(mini, tmp_path):
    again = run_gen(tmp_path / "again", fixture="mini")
    assert again.code == 0
    for sub in ("books", "truth"):
        for f in sorted((mini / sub).iterdir()):
            assert f.read_bytes() == (tmp_path / "again" / sub / f.name).read_bytes(), f.name
    other = run_gen(tmp_path / "other", "--seed", "7", fixture="mini")
    assert other.code == 0
    assert (mini / "books" / "usage_monthly.csv").read_bytes() != (
        tmp_path / "other" / "books" / "usage_monthly.csv").read_bytes()


def test_window_and_first_month_flags(tmp_path):
    result = run_gen(tmp_path, "--accounts", "60", "--months", "4", "--first-month", "2026-01")
    assert result.code == 0, result.stderr
    assert json.loads((tmp_path / "books" / "window.json").read_text()) == {
        "first": "2026-01", "last": "2026-04"}
    assert {r["gl_period"] for r in read_csv(tmp_path / "books" / "gl.csv")} == {
        "2026-01", "2026-02", "2026-03", "2026-04"}
    assert run_gen(tmp_path / "few", "--accounts", "10").code == 2


def test_manifest_header_carries_the_config_hashes(full):
    manifest = (full / "books.manifest.sha256").read_text().splitlines()
    header = dict(line[2:].split(": ", 1) for line in manifest if line.startswith("# ") and ": " in line)
    assert header["policy_sha256"] == gen_config.sha256_file(CONFIG / "policy.yaml")
    assert header["allocation_sha256"] == gen_config.sha256_file(CONFIG / "allocation.yaml")
    assert header["seed"] == "20261013" and header["accounts"] == "300" and header["months"] == "12"
    listed = [line.split("  ")[1] for line in manifest if not line.startswith("#")]
    assert listed == sorted(["accounts.csv", "cloud_focus.csv", "gl.csv", "invoices.csv",
                             "provider_invoices.csv", "support_tickets.csv", "usage_monthly.csv",
                             "window.json"])


def test_full_gen_leaves_nested_books_alone(tmp_path):
    nested = tmp_path / "books" / "small"
    nested.mkdir(parents=True)
    (nested / "keep.csv").write_text("x\n")
    (tmp_path / "books" / "small.manifest.sha256").write_text("# another books directory\n")
    assert run_gen(tmp_path, "--accounts", "40", "--months", "2").code == 0
    assert (nested / "keep.csv").is_file()
    manifest = (tmp_path / "books.manifest.sha256").read_text()
    assert "small" not in manifest
    assert run_gen(tmp_path, "--verify").code == 0


# ------------------------------------------------------------------ the verify check


def test_verify_passes_on_untouched_books(mini):
    result = run_gen(mini, "--verify", fixture="mini")
    assert result.code == 0, result.stderr
    assert result.json["status"] == "ok"


def test_fixture_mini_verifies_with_no_other_flags():
    result = margin("gen", "--json", "--fixture", "mini", "--verify")
    assert result.code == 0, result.stderr
    assert result.json["status"] == "ok"


def test_editing_floor_pct_fails_verify(mini, tmp_path):
    config = tmp_path / "config"
    shutil.copytree(CONFIG, config)
    text = (config / "policy.yaml").read_text()
    assert "floor_pct: 30\n" in text
    (config / "policy.yaml").write_text(text.replace("floor_pct: 30\n", "floor_pct: 31\n"))
    result = run_gen(mini, "--verify", config=config, fixture="mini")
    assert result.code == 3
    (failed,) = result.json["failed_controls"]
    assert failed["id"] == "CHK-GEN-VERIFY"
    assert result.json["message"] == (
        f"config changed since the books were generated (compared {config}): regenerate"
    )


def test_editing_the_allocation_config_fails_verify(mini, tmp_path):
    config = tmp_path / "config"
    shutil.copytree(CONFIG, config)
    text = (config / "allocation.yaml").read_text()
    (config / "allocation.yaml").write_text(text.replace("recommended: by_tokens", "recommended: by_revenue"))
    result = run_gen(mini, "--verify", config=config, fixture="mini")
    assert result.code == 3
    assert result.json["failed_controls"][0]["id"] == "CHK-GEN-VERIFY"


def test_regenerating_clears_the_verify_failure(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(CONFIG, config)
    assert run_gen(tmp_path, config=config, fixture="scenario").code == 0
    (config / "policy.yaml").write_text(
        (config / "policy.yaml").read_text().replace("floor_pct: 30\n", "floor_pct: 28\n"))
    assert run_gen(tmp_path, "--verify", config=config, fixture="scenario").code == 3
    assert run_gen(tmp_path, config=config, fixture="scenario").code == 0
    assert run_gen(tmp_path, "--verify", config=config, fixture="scenario").code == 0


def test_a_changed_price_list_fails_verify(scenario, tmp_path):
    other = tmp_path / "prices.csv"
    other.write_text(PRICES.read_text().replace("10.00,30.00", "10.50,30.00"))
    result = run_gen(scenario, "--verify", prices=other, fixture="scenario")
    assert result.code == 3
    assert "price list changed" in result.json["message"]


def test_verify_catches_an_edited_books_file(tmp_path):
    assert run_gen(tmp_path, fixture="scenario").code == 0
    gl = tmp_path / "books" / "gl.csv"
    gl.write_text(gl.read_text().replace(",5300,", ",5300,1", 1))
    result = run_gen(tmp_path, "--verify", fixture="scenario")
    assert result.code == 3
    assert "books on disk differ" in result.json["message"]


def test_verify_catches_edited_truth(tmp_path):
    assert run_gen(tmp_path, fixture="scenario").code == 0
    flags = tmp_path / "truth" / "rule_flags.csv"
    flags.write_text(flags.read_text().replace("true", "false", 1))
    result = run_gen(tmp_path, "--verify", fixture="scenario")
    assert result.code == 3 and "truth on disk differs" in result.json["message"]


def test_verify_without_a_manifest_is_a_precondition(tmp_path):
    assert run_gen(tmp_path, "--verify").code == 4


# ------------------------------------------------------------------ the books tie to the ledger


def test_gl_totals_equal_the_component_sums_for_all_12_months(full):
    books = full / "books"
    results = derive(books)
    assert len(results) == 12
    gl: dict[tuple[str, str], int] = {}
    for r in read_csv(books / "gl.csv"):
        key = (r["gl_period"], r["gl_code"])
        gl[key] = gl.get(key, 0) + int(r["amount_cents"])
    provider: dict[str, int] = {}
    for r in read_csv(books / "provider_invoices.csv"):
        provider[r["month"]] = provider.get(r["month"], 0) + int(r["amount_cents"])
    effective: dict[str, int] = {}
    for r in read_csv(books / "cloud_focus.csv"):
        effective[r["ChargePeriodStart"][:7]] = effective.get(r["ChargePeriodStart"][:7], 0) + cents(r["EffectiveCost"])
    tickets: dict[str, int] = {}
    rates = ref.read_rates(RATES)
    for r in read_csv(books / "support_tickets.csv"):
        if r["closed_date"]:
            tickets[r["closed_date"][:7]] = tickets.get(r["closed_date"][:7], 0) + int(r["handle_minutes"]) * rates[r["tier"]]
    for month, res in results.items():
        comps = res.comps.values()
        assert sum(c.revenue for c in comps) == gl[(month, "4000")] + gl[(month, "4100")] + gl[(month, "4900")]
        assert sum(c.inference for c in comps) == gl[(month, "5100")] == provider[month]
        assert sum(c.direct for c in comps) + res.pool_cents == gl[(month, "5200")] == effective[month]
        assert sum(c.support for c in comps) == gl[(month, "5300")] == tickets[month]
        for rule in gen_config.KNOWN_RULES:
            assert sum(c.shared[rule] for c in comps) == res.pool_cents
        for c in comps:
            for rule, profit in c.profit.items():
                assert profit == c.revenue - c.inference - c.direct - c.shared[rule] - c.support


def test_the_ledger_support_total_is_independent_of_the_rates_file(full, tmp_path):
    # GL 5300 comes from the generator's own ledger; a one-cent-per-minute slip in the rates the
    # close reads makes the two figures differ, so the tie-out can fail.
    slipped = tmp_path / "rates.csv"
    slipped.write_text(RATES.read_text().replace("standard,90,", "standard,91,"))
    books = full / "books"
    gl_5300 = sum(int(r["amount_cents"]) for r in read_csv(books / "gl.csv")
                  if r["gl_code"] == "5300" and r["gl_period"] == MARCH)
    config = gen_config.load_config(CONFIG)
    res = ref.recompute_from_books(books, PRICES, slipped, config.allocation.rules,
                                   config.policy.floor_pct, [(d.code, d.median_floor) for d in config.policy.drivers])
    assert sum(c.support for c in res[MARCH].comps.values()) != gl_5300


def test_truth_cost_components_match_the_books(full):
    results = derive(full / "books")
    rows = read_csv(full / "truth" / "cost_components.csv")
    assert len(rows) == sum(len(r.comps) for r in results.values())
    for row in rows:
        c = results[row["month"]].comps[row["account_id"]]
        assert int(row["revenue_cents"]) == c.revenue
        assert int(row["inference_cost_cents"]) == c.inference
        assert int(row["cloud_direct_cents"]) == c.direct
        assert int(row["support_cost_cents"]) == c.support
        for rule in c.shared:
            assert int(row[f"cloud_shared_{rule}_cents"]) == c.shared[rule]
            assert int(row[f"contribution_profit_{rule}_cents"]) == c.profit[rule]


def test_focus_rows_are_two_decimal_usd_within_one_month(full):
    rows = read_csv(full / "books" / "cloud_focus.csv")
    accounts = {r["account_id"] for r in read_csv(full / "books" / "accounts.csv")}
    tagged = untagged = shared = 0
    for r in rows:
        assert r["BillingCurrency"] == "USD"
        for column in ("BilledCost", "EffectiveCost", "ListCost", "ContractedCost"):
            assert len(r[column].split(".")[1]) == 2
        start, end = r["ChargePeriodStart"], r["ChargePeriodEnd"]
        assert start.endswith("Z") and end.endswith("Z") and start[:7] <= end[:7]
        assert end <= (f"{int(start[:4]) + (start[5:7] == '12'):04d}-"
                       f"{1 if start[5:7] == '12' else int(start[5:7]) + 1:02d}-01T00:00:00Z")
        assert r["RegionId"]
        tags = json.loads(r["Tags"])
        if tags:
            assert tags["product"] in ("api", "batch", "dedicated")
            assert tags["team"] in ("enterprise", "growth", "self_serve")
        tenant = tags.get("tenant")
        if tenant == "shared":
            shared += 1
        elif tenant:
            assert tenant in accounts and tags["product"] == "dedicated"
            tagged += 1
        else:
            assert tags == {}
            untagged += 1
    assert tagged and untagged and shared
    assert len({r["line_id"] for r in rows}) == len(rows)


def test_billed_equals_effective_per_month_and_commitment_rows_follow_the_window(full):
    rows = read_csv(full / "books" / "cloud_focus.csv")
    months = sorted({r["ChargePeriodStart"][:7] for r in rows})
    assert len(months) == 12
    for month in months:
        in_month = [r for r in rows if r["ChargePeriodStart"].startswith(month)]
        assert sum(cents(r["BilledCost"]) for r in in_month) == sum(cents(r["EffectiveCost"]) for r in in_month)
        purchases = [r for r in in_month if r["ChargeCategory"] == "Purchase"]
        covered = [r for r in in_month if r["ChargeCategory"] == "Usage" and r["CommitmentDiscountCategory"]]
        if month == months[0]:
            assert not purchases and not covered
            continue
        assert len(purchases) == 1 and covered
        assert cents(purchases[0]["EffectiveCost"]) > 0
        assert json.loads(purchases[0]["Tags"])["tenant"] == "shared"
        for r in covered:
            assert cents(r["BilledCost"]) == 0 and cents(r["ContractedCost"]) > cents(r["EffectiveCost"])
        for r in in_month:
            assert cents(r["ListCost"]) >= cents(r["ContractedCost"]) >= cents(r["EffectiveCost"])


def test_the_planted_untagged_share_is_two_points_below_the_threshold_every_month(full, policy):
    rows = read_csv(full / "books" / "cloud_focus.csv")
    for month in sorted({r["ChargePeriodStart"][:7] for r in rows}):
        usage = [r for r in rows if r["ChargePeriodStart"].startswith(month) and r["ChargeCategory"] == "Usage"]
        untagged = sum(cents(r["EffectiveCost"]) for r in usage if not json.loads(r["Tags"]))
        total = sum(cents(r["EffectiveCost"]) for r in usage)
        assert untagged > 0
        assert untagged * 100 <= (policy.tag_coverage_max_untagged_pct - 2) * total, month


def test_every_invoice_line_names_one_of_the_three_products(full):
    lines = read_csv(full / "books" / "invoices.csv")
    assert {r["product"] for r in lines} == {"api", "batch", "dedicated"}
    by_account: dict[str, set[str]] = {}
    for r in lines:
        by_account.setdefault(r["account_id"], set()).add(r["product"])
    assert any(len(products) > 1 for products in by_account.values())


def test_load_policy_requires_the_untagged_threshold_as_an_integer(tmp_path):
    text = (CONFIG / "policy.yaml").read_text()
    assert gen_config.load_policy(CONFIG).tag_coverage_max_untagged_pct == 10
    for name, body in (("missing", text.replace("tag_coverage_max_untagged_pct: 10", "")),
                       ("fraction", text.replace("tag_coverage_max_untagged_pct: 10", "tag_coverage_max_untagged_pct: 7.5"))):
        config = tmp_path / name
        config.mkdir()
        (config / "policy.yaml").write_text(body)
        (config / "allocation.yaml").write_text((CONFIG / "allocation.yaml").read_text())
        with pytest.raises(MarginError) as err:
            gen_config.load_policy(config)
        assert err.value.exit_code == 2 and "tag_coverage_max_untagged_pct" in str(err.value)


def test_tickets_have_tiers_dates_and_some_are_open(full):
    tickets = read_csv(full / "books" / "support_tickets.csv")
    tiers = set(gen_plan.SUPPORT_RATES)
    assert {t["tier"] for t in tickets} <= tiers
    assert all(int(t["handle_minutes"]) >= 1 for t in tickets)
    assert all(not t["closed_date"] or t["opened_date"] <= t["closed_date"] for t in tickets)
    assert any(not t["closed_date"] for t in tickets)  # still open at the end of the window


def test_accounts_are_synthetic_and_discounts_are_valid(full):
    accounts = read_csv(full / "books" / "accounts.csv")
    assert len(accounts) == 300 and len({a["display_name"] for a in accounts}) == 300
    assert all(0 <= Decimal(a["discount_pct"]) < 1 for a in accounts)
    assert {a["plan"] for a in accounts} == {"usage", "committed", "enterprise"}
    discounts = {r["account_id"]: r["discount_pct"] for r in read_csv(full / "truth" / "discounts.csv")}
    assert discounts == {a["account_id"]: a["discount_pct"] for a in accounts}


def test_usage_truth_matches_the_books_and_prices_are_listed(full):
    books, truth = full / "books", full / "truth"
    usage = sorted((r["month"], r["account_id"], r["model"], r["input_tokens"], r["output_tokens"])
                   for r in read_csv(books / "usage_monthly.csv"))
    assert usage == sorted((r["month"], r["account_id"], r["model"], r["input_tokens"], r["output_tokens"])
                           for r in read_csv(truth / "usage_by_model.csv"))
    assert {r["model"] for r in read_csv(truth / "list_prices.csv")} == {r["model"] for r in read_csv(PRICES)}
    rules = json.loads((truth / "allowed_rules.json").read_text())
    assert rules["rules"] == ["by_tokens", "by_revenue", "equal_usage"] and rules["recommended"] == "by_tokens"


# ------------------------------------------------------------------ planted flags


def test_the_planted_firm_set_is_non_empty_every_month_with_every_reason(full):
    planted = json.loads((full / "truth" / "planted_flags.json").read_text())
    months = [m for m in planted if m != "decoys"]
    assert len(months) == 12
    for month in months:
        firm = planted[month]
        assert 10 <= len(firm) <= 14, month
        assert {f["reason_code"] for f in firm} == set(gen_plan.REASONS)
        assert 6 <= len(planted["decoys"][month]) <= 10, month


def test_accounts_enter_and_exit_the_firm_set_between_march_and_april(full):
    planted = json.loads((full / "truth" / "planted_flags.json").read_text())
    march = {f["account_id"] for f in planted[MARCH]}
    april = {f["account_id"] for f in planted[APRIL]}
    assert april - march and march - april
    march_decoys, april_decoys = set(planted["decoys"][MARCH]), set(planted["decoys"][APRIL])
    assert april_decoys - march_decoys and march_decoys - april_decoys


def test_the_planted_set_is_what_the_predicate_derives_from_the_books(full):
    results = derive(full / "books")
    planted = json.loads((full / "truth" / "planted_flags.json").read_text())
    rule_rows = read_csv(full / "truth" / "rule_flags.csv")
    for month, res in results.items():
        firm = {k: res.reasons[k].code for k, c in res.comps.items() if c.flag == "firm"}
        assert firm == {f["account_id"]: f["reason_code"] for f in planted[month]}
        assert sorted(firm) == [f["account_id"] for f in planted[month]]
    assert len(rule_rows) == sum(len(r.comps) for r in results.values())
    for row in rule_rows:
        c = results[row["month"]].comps[row["account_id"]]
        assert {r: row[f"below_{r}"] == "true" for r in c.below} == c.below
        assert int(row["rules_below_floor"]) == c.rules_below and row["flag"] == c.flag


def test_decoys_are_exactly_the_soft_accounts(full):
    results = derive(full / "books")
    planted = json.loads((full / "truth" / "planted_flags.json").read_text())
    for month, res in results.items():
        soft = sorted(k for k, c in res.comps.items() if c.flag == "soft")
        assert soft == planted["decoys"][month]
        for k in soft:
            below = res.comps[k].rules_below
            assert 0 < below < len(res.comps[k].below)


def check_bands(results: dict[str, ref.MonthResult], planted: dict, policy: gen_config.Policy) -> None:
    floor = Fraction(policy.floor_pct)
    for month, res in results.items():
        firm = {f["account_id"] for f in planted[month]}
        decoys = set(planted["decoys"][month])
        for key, c in res.comps.items():
            if c.revenue <= 0:  # below by arithmetic: no margin to bound
                assert key in firm and all(p < 0 for p in c.profit.values())
                continue
            pts = points(c)
            if key in firm:
                assert all(p <= floor - Fraction(policy.planted_band_pts) for p in pts), (month, key)
                assert c.flag == "firm"
            elif key in decoys:
                assert all(abs(p - floor) >= Fraction(policy.decoy_band_pts) for p in pts), (month, key)
            else:
                assert all(p >= floor + Fraction(policy.unplanted_band_pts) for p in pts), (month, key)
                assert c.flag == "none"


def test_every_account_respects_its_guard_band_on_the_full_books(full, policy):
    check_bands(derive(full / "books"), json.loads((full / "truth" / "planted_flags.json").read_text()), policy)


@pytest.mark.parametrize("name", ["mini", "scenario"])
def test_presets_respect_their_guard_bands(name, request, policy):
    root = request.getfixturevalue(name)
    check_bands(derive(root / "books"), json.loads((root / "truth" / "planted_flags.json").read_text()), policy)


def test_guard_bands_follow_the_config(tmp_path, policy):
    # Tighter bands in a copy of the config are honoured: the bands are read, not built in.
    config = tmp_path / "config"
    shutil.copytree(CONFIG, config)
    text = (config / "policy.yaml").read_text().replace("planted_band_pts: 5", "planted_band_pts: 9")
    text = text.replace("unplanted_band_pts: 3", "unplanted_band_pts: 6")
    (config / "policy.yaml").write_text(text)
    result = run_gen(tmp_path / "out", "--accounts", "60", "--months", "3", config=config)
    assert result.code == 0, result.stderr
    tight = gen_config.load_config(config).policy
    assert tight.planted_band_pts == 9
    check_bands(derive(tmp_path / "out" / "books", config),
                json.loads((tmp_path / "out" / "truth" / "planted_flags.json").read_text()), tight)


# ------------------------------------------------------------------ the classifier


def test_every_planted_reason_survives_the_classifier(full, policy):
    results = derive(full / "books")
    planted = json.loads((full / "truth" / "planted_flags.json").read_text())
    zero_median_drivers = set()
    for month, res in results.items():
        for f in planted[month]:
            got = res.reasons[f["account_id"]]
            assert got.code == f["reason_code"], (month, f)
            if got.gap is not None:
                assert got.gap >= policy.reason_margin, (month, f, got.gap)
        cohort = [k for k, a in res.accts.items() if a.has_usage and res.comps[k].revenue > 0]
        for code in ("DEEP_DISCOUNT", "DEDICATED_CLOUD"):
            values = [ref.driver_values(res.accts[k], res.comps[k])[code] for k in cohort]
            if ref.lower_median(values) == 0:
                zero_median_drivers.add(code)
    # Half the accounts have no discount and few have a tagged cost, so those medians are zero
    # and the median_floor is what makes the multiple finite.
    assert zero_median_drivers == {"DEEP_DISCOUNT", "DEDICATED_CLOUD"}


@pytest.mark.parametrize("name", ["mini", "scenario"])
def test_presets_reasons_survive_the_classifier(name, request, policy):
    root = request.getfixturevalue(name)
    results = derive(root / "books")
    planted = json.loads((root / "truth" / "planted_flags.json").read_text())
    for month, res in results.items():
        assert planted[month]
        for f in planted[month]:
            assert res.reasons[f["account_id"]].code == f["reason_code"], (month, f)


def _acct(id_, *, fee=0, usage=0, credit=0, discount="0", tokens=True, direct=0, support=0):
    return ref.AcctMonth(id_, Decimal(discount), fee, usage, credit,
                         {"m": (1000, 1000)} if tokens else {}, direct, support)


def _comp(a: ref.AcctMonth, inference: int) -> ref.Components:
    profit = ref.contribution_cents(a.revenue_cents, inference, a.direct_cents, 0, a.support_cents)
    return ref.Components(a.revenue_cents, inference, a.direct_cents, a.support_cents,
                          {"r": 0}, {"r": profit}, {"r": ref.below(profit, a.revenue_cents, 30)})


DRIVERS = [("DEEP_DISCOUNT", Decimal("0.01")), ("MODEL_MIX", Decimal("0.01")),
           ("SUPPORT_LOAD", Decimal("0.01")), ("DEDICATED_CLOUD", Decimal("0.01"))]


def classify(accts_with_inference: dict[str, tuple[ref.AcctMonth, int]]):
    accts = {k: a for k, (a, _) in accts_with_inference.items()}
    comps = {k: _comp(a, i) for k, (a, i) in accts_with_inference.items()}
    return ref.classify_month(accts, comps, DRIVERS)


def test_classifier_truth_table():
    base = {f"n{i}": (_acct(f"n{i}", usage=10_000), 4_000) for i in range(5)}
    # A zero median: nobody else has a discount, so the floored median 0.01 gives the multiple.
    out = classify({**base, "d": (_acct("d", usage=5_000, discount="0.50"), 2_000)})
    assert out["d"].code == "DEEP_DISCOUNT" and out["d"].multiples["DEEP_DISCOUNT"] == Decimal("50")
    # Zero revenue skips the ratios: the largest cost in cents wins; an exact tie goes in order.
    zero = _acct("z", direct=700, support=300)
    assert classify({**base, "z": (zero, 100)})["z"].code == "DEDICATED_CLOUD"
    assert classify({**base, "z": (_acct("z", direct=500, support=500), 500)})["z"].code == "MODEL_MIX"
    assert classify({**base, "z": (_acct("z", direct=400, support=500), 100)})["z"].code == "SUPPORT_LOAD"
    # Negative revenue (credits exceed charges) is classified the same way.
    negative = _acct("g", fee=5_000, credit=-9_000)
    assert classify({**base, "g": (negative, 3_000)})["g"].code == "MODEL_MIX"
    # A platform-fee-only account has a list-price base but no usage row: ratios, not cents.
    fee_only = _acct("f", fee=20_000, tokens=False, support=16_000)
    out = classify({**base, "f": (fee_only, 0)})
    assert out["f"].code == "SUPPORT_LOAD" and out["f"].gap > 1
    # Nothing exceeds its cohort median: NONE.
    assert classify(base)["n0"].code == "NONE"
    # An exact tie between two drivers goes to the earlier in the order.
    tie = _acct("t", usage=10_000, support=1_000, direct=1_000)
    out = classify({**base, "t": (tie, 4_000)})
    assert out["t"].multiples["SUPPORT_LOAD"] == out["t"].multiples["DEDICATED_CLOUD"]
    assert out["t"].code == "SUPPORT_LOAD"


def test_list_price_revenue_restores_usage_but_not_the_platform_fee():
    # 10,000 cents of usage at a 20% discount was 12,500 at list; the 5,000 fee has no discount.
    assert ref.list_price_revenue(5_000, 10_000, Decimal("0.20")) == Decimal(17_500)
    assert ref.list_price_revenue(5_000, 0, Decimal("0.20")) == Decimal(5_000)


def test_median_is_the_lower_median_and_percentiles_are_nearest_rank():
    vals = [Decimal(x) for x in (4, 1, 3, 2)]
    assert ref.lower_median(vals) == 2 and ref.lower_median([Decimal(7)]) == 7 and ref.lower_median([]) == 0
    assert ref.nearest_rank(vals, Decimal("0.25")) == 1
    assert ref.nearest_rank(vals, Decimal("0.5")) == 2
    assert ref.nearest_rank(vals, Decimal("0.75")) == 3
    assert ref.nearest_rank(vals, Decimal("1")) == 4


def test_predicate_truth_table_at_one_cent_edges():
    # 30% of 10,000 is 3,000: the predicate is strict, and one cent either side flips it.
    assert ref.below(2_999, 10_000, 30) and not ref.below(3_000, 10_000, 30)
    assert not ref.below(3_001, 10_000, 30)
    # Zero revenue with cost is below by arithmetic; with no cost it is not.
    assert ref.below(-1, 0, 30) and not ref.below(0, 0, 30)
    # Negative revenue with any cost is below too: profit is at most the revenue, and 100R < 30R.
    assert ref.below(-100, -100, 30) and ref.below(-4_500, -4_000, 30)
    # A rounded margin of 30.0000 does not decide it: 29.99999 is below although it prints 30.0000.
    assert ref.below(2_999_999, 10_000_000, 30) and ref.margin_pct(2_999_999, 10_000_000) == Decimal("30.0000")
    assert ref.margin_pct(1, 0) is None


def test_margin_pct_rounds_half_even():
    assert ref.margin_pct(1, 32_000) == Decimal("0.0031")  # 0.003125 -> 0.0031
    assert ref.margin_pct(3, 32_000) == Decimal("0.0094")  # 0.009375 -> 0.0094
    assert ref.margin_pct(5, 32_000) == Decimal("0.0156")  # 0.015625 -> 0.0156


# ------------------------------------------------------------------ apportionment


@given(
    st.integers(min_value=0, max_value=10**9),
    st.dictionaries(st.text("abcdefgh", min_size=1, max_size=3), st.integers(min_value=0, max_value=10**12),
                    min_size=1, max_size=12),
)
def test_apportion_conserves_the_pool_and_floors_by_share(total, weights):
    if total and not any(w > 0 for w in weights.values()):
        with pytest.raises(ValueError):
            ref.apportion(total, weights)
        return
    out = ref.apportion(total, weights)
    assert sum(out.values()) == total
    weight_sum = sum(w for w in weights.values() if w > 0)
    for key, w in weights.items():
        assert out[key] >= 0 and (w > 0 or out[key] == 0)
        if total:
            assert total * w // weight_sum <= out[key] <= total * w // weight_sum + 1


def test_apportion_breaks_ties_by_ascending_account_id():
    assert ref.apportion(1, {"acct_0003": 1, "acct_0001": 1, "acct_0002": 1}) == {
        "acct_0001": 1, "acct_0002": 0, "acct_0003": 0}
    assert ref.apportion(2, {"b": 1, "a": 1, "c": 1}) == {"a": 1, "b": 1, "c": 0}
    # Largest remainder first: 10 cents over 1:1:2 gives 2.5, 2.5, 5 -> the tie goes to "a".
    assert ref.apportion(10, {"a": 1, "b": 1, "c": 2}) == {"a": 3, "b": 2, "c": 5}
    assert ref.apportion(0, {"a": 0}) == {"a": 0}


def test_shared_weights_follow_the_pool_table():
    accts = {
        "a": _acct("a", usage=1_000),
        "b": _acct("b", fee=500, tokens=False),
        "c": _acct("c", usage=300, credit=-900),
    }
    assert ref.shared_weights("by_tokens", accts) == {"a": 2000, "b": 0, "c": 2000}
    assert ref.shared_weights("by_revenue", accts) == {"a": 1000, "b": 500, "c": 0}  # negative revenue is 0
    assert ref.shared_weights("equal_usage", accts) == {"a": 1, "b": 0, "c": 1}
    with pytest.raises(ValueError):
        ref.shared_weights("by_magic", accts)


# ------------------------------------------------------------------ the forced content of the presets


def test_mini_has_each_required_property(mini):
    books = mini / "books"
    results = derive(books)
    assert sorted(results) == ["2026-02", "2026-03", "2026-04"]
    assert len(read_csv(books / "accounts.csv")) == 12
    assert json.loads((books / "window.json").read_text()) == {"first": "2026-02", "last": "2026-04"}
    march = results[MARCH]
    zero = march.comps["acct_0010"]
    assert zero.revenue == 0 and zero.inference + zero.direct + zero.support > 0 and zero.flag == "firm"
    fee_only = march.accts["acct_0011"]
    assert fee_only.fee_cents > 0 and not fee_only.has_usage and fee_only.usage_cents == 0
    assert march.comps["acct_0011"].support > 0 and march.comps["acct_0011"].flag == "firm"
    assert march.comps["acct_0012"].revenue < 0 and march.comps["acct_0012"].flag == "firm"
    assert march.comps["acct_0009"].flag == "soft"
    planted = json.loads((mini / "truth" / "planted_flags.json").read_text())
    entering = {f["account_id"] for f in planted[APRIL]} - {f["account_id"] for f in planted[MARCH]}
    exiting = {f["account_id"] for f in planted[MARCH]} - {f["account_id"] for f in planted[APRIL]}
    assert entering == {"acct_0007"} and exiting == {"acct_0008"}
    invoices = read_csv(books / "invoices.csv")
    for month in results:
        lines = [r for r in invoices if r["service_period"] == month]
        assert any(r["line_type"] == "credit" and int(r["amount_cents"]) < 0 for r in lines)
        assert any(r["line_type"] == "usage" for r in lines)
        assert any(not json.loads(r["Tags"]) for r in read_csv(books / "cloud_focus.csv")
                   if r["ChargePeriodStart"].startswith(month))
        assert any(r["gl_code"] == "5100" and r["gl_period"] == month for r in read_csv(books / "gl.csv"))


def test_scenario_preset_plants_three_accounts_below_the_floor_under_the_signed_rule(scenario):
    results = derive(scenario / "books")
    assert list(results) == [MARCH]
    comps = results[MARCH].comps
    assert sorted(comps) == ["acct_0001", "acct_0002", "acct_0003"]
    recommended = json.loads((scenario / "truth" / "allowed_rules.json").read_text())["recommended"]
    assert all(c.below[recommended] for c in comps.values())
    planted = json.loads((scenario / "truth" / "planted_flags.json").read_text())
    assert {f["account_id"]: f["reason_code"] for f in planted[MARCH]} == {
        "acct_0001": "MODEL_MIX", "acct_0002": "SUPPORT_LOAD", "acct_0003": "DEDICATED_CLOUD"}


def test_the_small_fixture_still_plants_nothing_and_reads_no_config(tmp_path):
    result = margin("gen", "--fixture", "small", "--out", str(tmp_path / "books"), "--json",
                    "--config", str(tmp_path / "no-such-config"))
    assert result.code == 0
    assert not (tmp_path / "truth").exists()
    assert not (tmp_path / "books" / "provider_invoices.csv").exists()
