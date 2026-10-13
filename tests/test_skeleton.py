"""Phase 0 walking skeleton: gen -> load -> dbt -> control -> provenance -> explain -> memo."""

import re
import shutil
from pathlib import Path

import duckdb
import pytest

from conftest import Workspace, margin
from margin import explain as explain_mod
from margin import memo as memo_mod
from margin.books import write_manifest
from margin.errors import MarginError

TOTAL = 618985  # the small fixture's March revenue, in cents


def test_gen_is_deterministic(tmp_path):
    a, b, c = (Workspace(tmp_path / n) for n in "abc")
    assert a.gen().code == b.gen().code == 0
    assert c.gen("--seed", "7").code == 0
    for name in ["accounts.csv", "invoices.csv", "usage_monthly.csv", "gl.csv", "window.json"]:
        assert (a.books / name).read_bytes() == (b.books / name).read_bytes()
    assert (a.books / "invoices.csv").read_bytes() != (c.books / "invoices.csv").read_bytes()


def test_gen_needs_a_preset():
    result = margin("gen", "--json")
    assert result.code == 2


def test_books_tie_to_the_ledger_in_integer_cents(tmp_path):
    ws = Workspace(tmp_path)
    ws.gen()
    import csv

    invoices = list(csv.DictReader((ws.books / "invoices.csv").open()))
    gl = list(csv.DictReader((ws.books / "gl.csv").open()))
    assert sum(int(r["amount_cents"]) for r in invoices) == sum(int(r["amount_cents"]) for r in gl)
    assert sum(int(r["amount_cents"]) for r in invoices) == TOTAL


def test_close_is_idempotent(closed):
    first = closed.close().json
    second = closed.close().json
    assert first["result_hash"] == second["result_hash"]
    assert first["controls"] == [{"id": "CTL-TIE-REV", "status": "pass", "violating_rows": 0}]


def test_close_does_not_need_truth(closed, tmp_path):
    # The small fixture writes no truth/ at all, and close still succeeds (EC-01).
    assert not (closed.books.parent / "truth").exists()
    assert margin("close", "--month", "2026-03", "--check", *closed.flags()).code == 4


def test_month_outside_the_window_exits_2_naming_the_range(closed):
    result = closed.close("2025-09")
    assert result.code == 2
    assert "2026-03..2026-03" in result.json["message"]
    assert closed.close("2026-3").code == 2


def test_a_warehouse_is_tied_to_its_books(closed, tmp_path):
    other = Workspace(tmp_path)
    assert other.gen("--seed", "7").code == 0
    result = margin("close", "--month", "2026-03", "--json", "--books", str(other.books),
                    "--warehouse", str(closed.warehouse))
    assert result.code == 2
    assert result.json["message"] == "warehouse holds different books: use --warehouse or delete it"


def test_a_ledger_slip_fails_the_named_control_and_leaves_the_month_open(tmp_path):
    ws = Workspace(tmp_path)
    ws.gen()
    gl = ws.books / "gl.csv"
    text = gl.read_text()
    assert ",4100,461785," in text
    gl.write_text(text.replace(",4100,461785,", ",4100,461786,"))
    write_manifest(ws.books)

    result = ws.close()
    assert result.code == 3
    env = result.json
    assert env["status"] == "fail" and env["month"] == "2026-03"
    (failed,) = env["failed_controls"]
    assert failed["id"] == "CTL-TIE-REV" and failed["violating_rows"] == 1
    assert "pnl/2026-03/4100" in failed["detail"][0]
    assert re.search(r"rows [0-9a-f]{32}(,[0-9a-f]{32})+", failed["detail"][0])

    # The month never closed: readers refuse it (plan §6.5).
    assert margin("memo", "--month", "2026-03", "--out", str(tmp_path / "s"), *ws.flags()).code == 4
    assert margin("explain", "cp/2026-03/acct_0001/revenue", *ws.flags()).code == 4


def test_explain_ends_at_source_rows(closed):
    leaf = margin("explain", "cp/2026-03/acct_0001/revenue", "--json", *closed.flags()).json
    chain = leaf["chain"]
    assert chain["value_cents"] == 98771 and chain["digest_verified"] is True
    assert len(chain["rows"]) == 3
    assert {r["source_table"] for r in chain["rows"]} == {"raw.invoices"}
    assert all(re.fullmatch(r"[0-9a-f]{32}", r["_row_hash"]) for r in chain["rows"])

    rollup = margin("explain", "rollup/2026-03/revenue", "--json", *closed.flags()).json["chain"]
    assert rollup["value_cents"] == TOTAL
    assert [c["number_id"] for c in rollup["children"]] == [
        f"cp/2026-03/acct_000{n}/revenue" for n in (1, 2, 3)
    ]
    assert all(c["rows"] for c in rollup["children"])  # the default depth reaches the leaves

    shallow = margin("explain", "rollup/2026-03/revenue", "--depth", "0", "--json",
                     *closed.flags()).json["chain"]
    assert all("rows" not in c for c in shallow["children"])


def test_explain_unknown_id_names_the_nearest(closed):
    result = margin("explain", "cp/2026-03/acct_0001/revenu", "--json", *closed.flags())
    assert result.code == 2
    assert "nearest: cp/2026-03/acct_0001/revenue" in result.json["message"]


def test_explain_detects_a_forged_digest(closed_copy):
    con = duckdb.connect(str(closed_copy.warehouse))
    con.execute("UPDATE prov.records SET input_digest = repeat('0', 64) "
                "WHERE number_id = 'cp/2026-03/acct_0002/revenue'")
    con.close()
    result = margin("explain", "cp/2026-03/acct_0002/revenue", "--json", *closed_copy.flags())
    assert result.code == 6
    assert "digest mismatch" in result.json["message"]


def test_memo_numbers_all_resolve_through_explain(closed, tmp_path):
    out = tmp_path / "site"
    assert margin("memo", "--month", "2026-03", "--out", str(out), *closed.flags()).code == 0
    html = (out / "2026-03" / "index.html").read_text()
    ids = re.findall(r'data-num-id="([^"]+)" data-value="([^"]+)"', html)
    assert {i for i, _ in ids} >= {"rollup/2026-03/revenue", "cp/2026-03/acct_0003/revenue",
                                   "pnl/2026-03/4000"}
    con = duckdb.connect(str(closed.warehouse), read_only=True)
    try:
        for number_id, value in ids:
            node = explain_mod.explain(con, number_id)
            assert str(node["value_cents"]) == value
            assert node["digest_verified"]
    finally:
        con.close()
    assert (out / "index.html").is_file()


def test_memo_has_no_naked_numerals(closed, tmp_path):
    out = tmp_path / "site"
    margin("memo", "--month", "2026-03", "--out", str(out), *closed.flags())
    html = (out / "2026-03" / "index.html").read_text()
    body = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    body = re.sub(r"<span data-num-id.*?</span>", "", body)
    # Identifiers are labels, not figures: account IDs and the GL code column.
    body = re.sub(r"acct_\d{4}", "", body)
    body = re.sub(r"<td>(?:4000|4100|4900)</td>", "", body)
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\b\d{4}-\d{2}\b", " ", text)  # the month label is an ISO date
    assert not re.findall(r"\d", text), "a numeral outside the number registry"


def test_memo_refuses_an_unregistered_number():
    env = memo_mod.make_env({})
    with pytest.raises(MarginError) as err:
        env.from_string("{{ 'cp/2026-03/acct_0001/revenue' | num }}").render()
    assert err.value.exit_code == 3
    assert err.value.failed_controls[0]["id"] == "CHK-MEMO-NUM"


def test_memo_escapes_account_names():
    registry = {"rollup/2026-03/revenue": {"value_cents": 1, "value_text": "1", "unit": "cents"}}
    html = memo_mod.render("2026-03", [{"account_id": "a", "display_name": "<script>x</script>"}],
                           {**registry, "cp/2026-03/a/revenue": registry["rollup/2026-03/revenue"],
                            **{f"pnl/2026-03/{c}": registry["rollup/2026-03/revenue"]
                               for c in ("4000", "4100", "4900")}})
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
