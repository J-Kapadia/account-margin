"""The close controls on tampered copies of the committed `mini` books: each tie-out, the price
pin and its FOCUS boundary rows, and allocation conservation. Every case closes 2026-03 in its
own warehouse and reads the named controls from the failure envelope.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from conftest import MINI_BOOKS, MINI_CONFIG, MINI_PRICES, margin

GL_CODE_CONTROL = {"5100": "CTL-TIE-INF", "5200": "CTL-TIE-CLD", "5300": "CTL-TIE-SUP"}


@pytest.fixture
def books(tmp_path) -> Path:
    """A private copy of the `mini` books and manifest, to tamper with."""
    target = tmp_path / "b" / "books"
    shutil.copytree(MINI_BOOKS, target)
    shutil.copy(MINI_BOOKS.parent / "books.manifest.sha256", target.parent / "books.manifest.sha256")
    return target


def rewrite(path: Path, change) -> None:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields, rows = reader.fieldnames, list(reader)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(change(rows) or rows)


def close(books: Path, tmp_path: Path, month: str = "2026-03", *, prices: Path = MINI_PRICES, extra=()):
    return margin(
        "close", "--fixture", "mini", "--books", str(books), "--warehouse", str(tmp_path / "w.duckdb"),
        "--config", str(MINI_CONFIG), "--month", month, "--json", *extra, env={"MARGIN_PRICES": str(prices)},
    )


def failed_ids(result) -> list[str]:
    return [f["id"] for f in result.json["failed_controls"]]


def slip_gl(books: Path, code: str, cents: int = 1, period: str = "2026-03") -> None:
    def change(rows):
        k = next(i for i, r in enumerate(rows) if r["gl_period"] == period and r["gl_code"] == code)
        rows[k] = {**rows[k], "amount_cents": str(int(rows[k]["amount_cents"]) + cents)}

    rewrite(books / "gl.csv", change)


def test_the_untouched_books_pass_every_control(books, tmp_path):
    result = close(books, tmp_path)
    assert result.code == 0, result.stderr
    assert {c["id"] for c in result.json["controls"]} >= {
        "CTL-ALLOC-CONSERVE", "CTL-PRICE-PIN", "CTL-TIE-CLD", "CTL-TIE-INF", "CTL-TIE-SUP"}
    assert all(c["status"] == "pass" for c in result.json["controls"])


@pytest.mark.parametrize("code", sorted(GL_CODE_CONTROL))
def test_a_one_cent_slip_in_a_cost_gl_code_fails_its_own_tie_out_and_only_it(books, tmp_path, code):
    slip_gl(books, code)
    result = close(books, tmp_path)
    assert result.code == 3
    assert failed_ids(result) == [GL_CODE_CONTROL[code]]
    (failed,) = result.json["failed_controls"]
    assert failed["violating_rows"] == 1
    (line,) = failed["detail"]
    assert line.startswith(f"pnl/2026-03/{code}:") and "difference -1 cents" in line and "rows " in line


def test_a_slip_in_the_support_ledger_names_the_ledger_row_and_the_tickets(books, tmp_path):
    slip_gl(books, "5300")
    result = close(books, tmp_path)
    (line,) = result.json["failed_controls"][0]["detail"]
    hashes = line.split("rows ")[1].split(",")
    assert len(hashes) > 1 and all(len(h) == 32 for h in hashes)  # the GL entry and the month's tickets


def test_ledger_support_cost_with_no_closed_ticket_is_a_failure_not_a_dropped_pool(books, tmp_path):
    rewrite(books / "support_tickets.csv", lambda rows: [r for r in rows if not r["closed_date"].startswith("2026-03")])
    result = close(books, tmp_path)
    assert result.code == 3 and failed_ids(result) == ["CTL-TIE-SUP"]
    assert "no ticket closed in the month" in result.json["failed_controls"][0]["detail"][0]


def test_ticket_cost_with_no_ledger_entry_is_a_failure(books, tmp_path):
    rewrite(books / "gl.csv", lambda rows: [r for r in rows if not (r["gl_period"] == "2026-03" and r["gl_code"] == "5300")])
    result = close(books, tmp_path)
    assert result.code == 3 and failed_ids(result) == ["CTL-TIE-SUP"]
    assert "the ledger has none" in result.json["failed_controls"][0]["detail"][0]


# ------------------------------------------------------------------ allocation


def test_an_empty_usage_month_fails_allocation_naming_the_month_rule_and_pool(books, tmp_path):
    rewrite(books / "usage_monthly.csv", lambda rows: [r for r in rows if r["month"] != "2026-03"])
    result = close(books, tmp_path)
    assert result.code == 3
    assert "CTL-ALLOC-CONSERVE" in failed_ids(result)
    alloc = next(f for f in result.json["failed_controls"] if f["id"] == "CTL-ALLOC-CONSERVE")
    text = "\n".join(alloc["detail"])
    assert "month 2026-03, rule by_tokens, pool shared cloud" in text
    assert "month 2026-03, rule equal_usage, pool shared cloud" in text
    assert "rule by_revenue" not in text  # revenue still weights that rule: no fallback needed
    assert "pool inference " in text  # every provider invoice, too
    assert alloc["violating_rows"] == len(alloc["detail"])
    assert result.json["month"] == "2026-03"


def test_a_tenant_tag_naming_no_account_fails_allocation(books, tmp_path):
    def change(rows):
        k = next(i for i, r in enumerate(rows) if r["ChargePeriodStart"].startswith("2026-03") and "tenant" in r["Tags"])
        rows[k] = {**rows[k], "Tags": '{"tenant":"acct_9999"}'}

    rewrite(books / "cloud_focus.csv", change)
    result = close(books, tmp_path)
    alloc = next(f for f in result.json["failed_controls"] if f["id"] == "CTL-ALLOC-CONSERVE")
    assert any("tenant tag names an unknown account" in d and d.startswith("rollup/2026-03/cloud_direct") for d in alloc["detail"])


def test_a_charge_both_direct_and_shared_fails_allocation(books, tmp_path):
    def change(rows):
        shared = next(r for r in rows if r["ChargePeriodStart"].startswith("2026-03") and "tenant" not in r["Tags"])
        rows.append({**shared, "Tags": '{"tenant":"acct_0001"}', "line_id": "focus_2026-03_dup"})

    rewrite(books / "cloud_focus.csv", change)
    result = close(books, tmp_path)
    assert result.code == 3
    alloc = next(f for f in result.json["failed_controls"] if f["id"] == "CTL-ALLOC-CONSERVE")
    assert len(alloc["detail"]) == 1
    assert alloc["detail"][0].startswith("cp/2026-03/acct_0001/cloud_direct:") and "direct and shared" in alloc["detail"][0]


def test_a_usage_model_with_no_provider_invoice_fails_allocation(books, tmp_path):
    rewrite(books / "provider_invoices.csv",
            lambda rows: [r for r in rows if not (r["month"] == "2026-03" and r["model"] == "fixture-small")])
    result = close(books, tmp_path)
    alloc = next(f for f in result.json["failed_controls"] if f["id"] == "CTL-ALLOC-CONSERVE")
    assert any("pool inference fixture-small" in d and "usage model has no provider invoice" in d for d in alloc["detail"])


# ------------------------------------------------------------------ price pin


def test_a_usage_model_with_no_pinned_price_fails_naming_the_model(books, tmp_path):
    prices = tmp_path / "prices.csv"
    lines = MINI_PRICES.read_text().splitlines()
    prices.write_text("\n".join(l for l in lines if "fixture-medium" not in l) + "\n")
    result = close(books, tmp_path, "2026-02", prices=prices)  # the first month of the window
    assert result.code == 3 and "CTL-PRICE-PIN" in failed_ids(result)
    pin = next(f for f in result.json["failed_controls"] if f["id"] == "CTL-PRICE-PIN")
    assert pin["violating_rows"] == 1
    assert pin["detail"][0].startswith("rollup/2026-02/inference_cost: model fixture-medium: no pinned price")
    assert result.json["month"] == "2026-02"


@pytest.mark.parametrize("edit, reason", [
    (lambda r: {**r, "source_url": ""}, "price has no source_url"),
    (lambda r: {**r, "retrieved_on": ""}, "price has no retrieved_on date"),
    (lambda r: {**r, "input_per_mtok": "0.0005"}, "whole number of nano-USD"),
    (lambda r: {**r, "output_per_mtok": "-1"}, "not a positive number"),
])
def test_a_price_without_a_source_a_date_or_a_whole_nano_price_is_not_pinned(books, tmp_path, edit, reason):
    prices = tmp_path / "prices.csv"
    shutil.copy(MINI_PRICES, prices)
    rewrite(prices, lambda rows: [edit(r) if r["model"] == "fixture-small" else r for r in rows])
    result = close(books, tmp_path, prices=prices)
    pin = next(f for f in result.json["failed_controls"] if f["id"] == "CTL-PRICE-PIN")
    assert len(pin["detail"]) == 1 and reason in pin["detail"][0] and "fixture-small" in pin["detail"][0]


def test_a_month_without_an_inference_side_needs_no_price(tmp_path):
    # The `small` books have usage rows, no provider invoices and no price list.
    assert margin("gen", "--fixture", "small", "--out", str(tmp_path / "books"), "--json").code == 0
    result = margin("close", "--month", "2026-03", "--books", str(tmp_path / "books"), "--json",
                    "--warehouse", str(tmp_path / "w.duckdb"), "--config", str(MINI_CONFIG))
    assert result.code == 0, result.stderr


def focus_end(books: Path, end: str) -> str:
    """Set the end of the first whole-month March FOCUS row; return its resource."""
    seen = {}

    def change(rows):
        k = next(i for i, r in enumerate(rows) if r["ChargePeriodStart"] == "2026-03-01T00:00:00Z")
        seen["resource"] = rows[k]["ResourceId"]
        rows[k] = {**rows[k], "ChargePeriodEnd": end}

    rewrite(books / "cloud_focus.csv", change)
    return seen["resource"]


def test_a_focus_row_ending_exactly_at_the_month_boundary_passes(books, tmp_path):
    focus_end(books, "2026-04-01T00:00:00Z")  # the first instant of the next month: end is exclusive
    result = close(books, tmp_path)
    assert result.code == 0, result.stderr
    assert next(c for c in result.json["controls"] if c["id"] == "CTL-PRICE-PIN")["status"] == "pass"


def test_a_focus_row_ending_a_second_past_the_boundary_fails(books, tmp_path):
    resource = focus_end(books, "2026-04-01T00:00:01Z")
    result = close(books, tmp_path)
    assert result.code == 3 and failed_ids(result) == ["CTL-PRICE-PIN"]
    (pin,) = result.json["failed_controls"]
    assert pin["violating_rows"] == 1
    assert resource in pin["detail"][0] and "ends after the first instant of the next month" in pin["detail"][0]


@pytest.mark.parametrize("field, value, reason", [
    ("BillingCurrency", "EUR", "BillingCurrency is not USD"),
    ("ChargePeriodEnd", "2026-04-01T00:00:00+02:00", "not a UTC timestamp with a Z suffix"),
    ("EffectiveCost", "12.345", "more than two decimals"),
    ("BilledCost", "12.345", "more than two decimals"),
])
def test_a_focus_row_that_is_not_usd_utc_or_two_decimals_fails(books, tmp_path, field, value, reason):
    def change(rows):
        k = next(i for i, r in enumerate(rows) if r["ChargePeriodStart"] == "2026-03-01T00:00:00Z")
        rows[k] = {**rows[k], field: value}

    rewrite(books / "cloud_focus.csv", change)
    result = close(books, tmp_path)
    pin = next(f for f in result.json["failed_controls"] if f["id"] == "CTL-PRICE-PIN")
    assert len(pin["detail"]) == 1 and reason in pin["detail"][0]
