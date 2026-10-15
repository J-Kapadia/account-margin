"""The provenance writer on the closed `mini` fixture: every mart number has a record whose digest
re-derives, the sign-off is stamped on it, and `result_hash` depends on values and digests only.
"""

from __future__ import annotations

import json
import shutil

import duckdb
import pytest

from conftest import FIXTURE_SIGNOFF, MINI_CONFIG, close_mini
from margin import prov, signoff

FAMILIES_IN_THE_HASH = {"cp", "pnl", "rollup", "flag", "cpr", "cohort"}


@pytest.fixture(scope="module")
def con(mini_closed):
    c = duckdb.connect(str(mini_closed), read_only=True)
    yield c
    c.close()


def _records(con):
    cols = ["number_id", "month", "value_cents", "value_text", "unit", "input_digest", "source_query"]
    return [dict(zip(cols, row)) for row in con.execute(f"SELECT {', '.join(cols)} FROM prov.records").fetchall()]


def test_every_digest_rederives_from_its_rows_or_its_children(con):
    stored = {r["number_id"]: r for r in _records(con)}
    for number_id, rec in stored.items():
        if prov.is_composite(rec["source_query"]):
            children = json.loads(rec["source_query"])
            derived = prov.digest_children([(c, stored[c]["input_digest"]) for c in children])
        else:
            derived = prov.digest_rows([row[1] for row in con.execute(rec["source_query"]).fetchall()])
        assert derived == rec["input_digest"], number_id


def test_every_id_is_in_the_grammar_and_month_less_ones_are_cfg_and_ref(con):
    for rec in _records(con):
        family = rec["number_id"].split("/", 1)[0]
        if rec["month"] is None:
            assert family in ("cfg", "ref"), rec["number_id"]
        else:
            assert family in FAMILIES_IN_THE_HASH
            assert prov.month_of_id(rec["number_id"]) == rec["month"].strftime("%Y-%m"), rec["number_id"]


def test_the_sign_off_is_stamped_on_every_record(con):
    (committed,) = FIXTURE_SIGNOFF.glob("*.json")
    signer = json.loads(committed.read_text())["signer"]
    assert con.execute(
        "SELECT count(*) FROM prov.records WHERE signer IS DISTINCT FROM ? OR signoff_ref IS DISTINCT FROM ?",
        [signer, committed.stem],
    ).fetchone()[0] == 0


def test_each_active_account_has_every_component_and_each_month_every_gl_code(con):
    for month in ("2026-02", "2026-03", "2026-04"):
        accounts = [a for (a,) in con.execute(
            "SELECT account_id FROM mart.fct_contribution_profit WHERE month = ?", [f"{month}-01"]).fetchall()]
        have = {r for (r,) in con.execute(
            "SELECT number_id FROM prov.records WHERE number_id LIKE ?", [f"cp/{month}/%"]).fetchall()}
        assert have == {f"cp/{month}/{a}/{c}" for a in accounts for c in prov.COMPONENTS}
        assert {r for (r,) in con.execute("SELECT number_id FROM prov.records WHERE number_id LIKE ?",
                                          [f"pnl/{month}/%"]).fetchall()} == {f"pnl/{month}/{c}" for c in prov.ALL_GL_CODES}
        assert {r for (r,) in con.execute("SELECT number_id FROM prov.records WHERE number_id LIKE ?",
                                          [f"flag/{month}/%"]).fetchall()} == {f"flag/{month}/{a}" for a in accounts}


def test_values_agree_with_the_marts(con):
    one = lambda sql, *args: con.execute(sql, list(args)).fetchone()[0]  # noqa: E731
    assert one("SELECT value_cents FROM prov.records WHERE number_id = 'rollup/2026-04/accounts_firm'") == one(
        "SELECT count(*) FROM mart.fct_flags WHERE month = DATE '2026-04-01' AND flag = 'firm'")
    assert one("SELECT value_cents FROM prov.records WHERE number_id = 'rollup/2026-04/contribution_profit'") == one(
        "SELECT sum(contribution_profit_cents) FROM mart.fct_contribution_profit WHERE month = DATE '2026-04-01'")
    assert one("SELECT value_cents FROM prov.records WHERE number_id = 'rollup/2026-04/accounts_zero_revenue'") == one(
        "SELECT count(*) FROM mart.fct_contribution_profit WHERE month = DATE '2026-04-01' AND revenue_cents = 0")
    assert one("SELECT value_cents FROM prov.records WHERE number_id = 'rollup/2026-04/accounts_status_change_by_tokens'") == 0
    unpriced = [a for (a,) in con.execute(
        "SELECT account_id FROM mart.fct_contribution_profit WHERE month = DATE '2026-04-01' AND margin_pct IS NULL"
    ).fetchall()]
    assert unpriced  # mini has a zero-revenue account
    for account in unpriced:
        assert one("SELECT value_text FROM prov.records WHERE number_id = ?", f"cp/2026-04/{account}/margin_pct") == "n/a"


def test_a_flag_names_its_reason_and_depends_on_every_rule_the_cohorts_and_the_configs(con):
    rows = con.execute(
        "SELECT number_id, value_text, source_query FROM prov.records WHERE number_id LIKE 'flag/2026-04/%'").fetchall()
    assert {text.split(":")[0] for _, text, _ in rows} <= {"firm", "soft", "none"}
    assert any(":" in text for _, text, _ in rows)
    number_id, _, children = rows[0]
    account = number_id.rsplit("/", 1)[1]
    assert set(json.loads(children)) == (
        {f"cpr/2026-04/{account}/{r}" for r in ("by_tokens", "by_revenue", "equal_usage")}
        | {f"cohort/2026-04/{c}_median" for c in ("deep_discount", "model_mix", "support_load", "dedicated_cloud")}
        | {"cfg/allocation", "cfg/policy"}
    )


def test_the_trailing_margin_covers_the_months_available(con):
    def children(month):
        (q,) = con.execute("SELECT source_query FROM prov.records WHERE number_id = ?",
                           [f"cp/{month}/acct_0001/trailing3_margin"]).fetchone()
        return {c.split("/")[1] for c in json.loads(q)}

    assert children("2026-02") == {"2026-02"}
    assert children("2026-04") == {"2026-02", "2026-03", "2026-04"}


def test_a_non_signed_rule_reads_its_own_weight_rows(con):
    def tables(sql):
        return {(t, h) for t, h, _ in con.execute(sql).fetchall()}

    by_tokens = tables(prov.cpr_query("acct_0001", "2026-04", "by_tokens"))
    by_revenue = tables(prov.cpr_query("acct_0001", "2026-04", "by_revenue"))
    others_invoices = {("raw.invoices", h) for (h,) in con.execute(
        "SELECT _row_hash FROM raw.invoices WHERE _month = DATE '2026-04-01' AND account_id <> 'acct_0001'").fetchall()}
    assert others_invoices <= by_revenue and not others_invoices & by_tokens
    signed_shared = tables(prov.component_query("cloud_shared", "acct_0001", "2026-04", "by_tokens"))
    assert not {t for t, _ in signed_shared} & {"raw.invoices"}


@pytest.mark.slow
def test_result_hash_is_equal_across_two_closes_and_ignores_the_commit(mini_closed, tmp_path):
    second = tmp_path / "second.duckdb"
    result = close_mini(second, env={"MARGIN_GIT_SHA": "0" * 40, "MARGIN_PRODUCER_KIND": "agent"})
    assert result.code == 0, result.stderr

    def hashes(path):
        with duckdb.connect(str(path), read_only=True) as c:
            return (
                c.execute("SELECT month, result_hash, input_digest FROM state.close_state ORDER BY month").fetchall(),
                c.execute("SELECT DISTINCT git_sha, producer_kind FROM prov.records").fetchall(),
            )

    (first_state, first_meta), (second_state, second_meta) = hashes(mini_closed), hashes(second)
    assert first_state == second_state and len(first_state) == 3
    assert second_meta == [("0" * 40, "agent")] and first_meta != second_meta  # stored, never hashed


def test_result_hash_ignores_the_month_less_records(con):
    records = _records(con)
    month = [r for r in records if r["month"] is not None and r["month"].strftime("%Y-%m") == "2026-04"]
    month_less = [r for r in records if r["month"] is None]
    assert month_less and prov.result_hash(month) == prov.result_hash(month + month_less)
    changed = [{**r, "value_text": "other", "input_digest": "0" * 64} for r in month_less]
    assert prov.result_hash(month + changed) == prov.result_hash(month)
    assert prov.result_hash(month) == con.execute(
        "SELECT result_hash FROM state.close_state WHERE month = DATE '2026-04-01'").fetchone()[0]


def test_result_hash_changes_with_a_value_or_a_digest():
    base = {"number_id": "cp/2026-04/a/revenue", "value_cents": 5, "value_text": "5", "unit": "cents",
            "input_digest": "d"}
    reference = prov.result_hash([base])
    assert prov.result_hash([{**base, "value_cents": 6}]) != reference
    assert prov.result_hash([{**base, "input_digest": "e"}]) != reference
    assert prov.result_hash([{**base, "run_id": "x", "git_sha": "y", "signer": "z"}]) == reference


def test_changing_one_pool_row_changes_every_cloud_shared_digest_and_no_other_component(mini_closed, tmp_path):
    from conftest import MINI_BOOKS
    import csv

    books = tmp_path / "b" / "books"
    shutil.copytree(MINI_BOOKS, books)
    shutil.copy(MINI_BOOKS.parent / "books.manifest.sha256", books.parent / "books.manifest.sha256")
    with (books / "cloud_focus.csv").open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields, rows = reader.fieldnames, list(reader)
    pool = next(r for r in rows if r["ChargePeriodStart"].startswith("2026-04") and '"tenant"' not in r["Tags"])
    pool["ResourceId"] += "-moved"
    with (books / "cloud_focus.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    from conftest import margin
    changed = tmp_path / "changed.duckdb"
    result = margin("close", "--fixture", "mini", "--books", str(books), "--warehouse", str(changed),
                    "--config", str(MINI_CONFIG), "--month", "2026-04", "--json")
    assert result.code == 0, result.stderr

    def digests(path):
        with duckdb.connect(str(path), read_only=True) as c:
            return dict(c.execute("SELECT number_id, input_digest FROM prov.records WHERE number_id LIKE 'cp/2026-04/%'").fetchall())

    before, after = digests(mini_closed), digests(changed)
    differing = {k.rsplit("/", 1)[1] for k in before if before[k] != after[k]}
    shared = {k for k in before if k.endswith("/cloud_shared")}
    assert shared and all(before[k] != after[k] for k in shared)
    assert not differing & {"revenue", "inference_cost", "cloud_direct", "support_cost"}
