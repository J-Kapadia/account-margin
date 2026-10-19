"""Importing an AWS Data Exports FOCUS 1.0 CSV into the books' `cloud_focus.csv`.

The fixture `tests/fixtures/focus/export.csv` is synthetic, typed from the specification's column
definitions; `export.totals.json` holds its per-month cost totals, computed outside the parser.
"""

from __future__ import annotations

import csv
import json
import random
import re
import shutil
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import MINI_BOOKS, MINI_CONFIG, MINI_PRICES, ROOT, connect, margin
from margin import dbt_runner, focus_import, load
from margin.errors import EXIT_USAGE, MarginError
from margin.focus_import import import_focus
from margin.warehouse import init_schemas

FIXTURE = ROOT / "tests" / "fixtures" / "focus"
EXPORT = FIXTURE / "export.csv"
TOTALS = json.loads((FIXTURE / "export.totals.json").read_text(encoding="utf-8"))
COSTS = ["ListCost", "ContractedCost", "EffectiveCost", "BilledCost"]
EXTRA = ["ChargeDescription", "PricingUnit", "ProviderName"]
MANDATORY = ["ChargePeriodStart", "ChargePeriodEnd", "BilledCost", "EffectiveCost", "BillingCurrency",
             "ServiceName", "ChargeCategory"]
OPTIONAL = ["ServiceCategory", "ResourceId", "Tags", "ListCost", "ContractedCost",
            "CommitmentDiscountCategory", "CommitmentDiscountType", "RegionId"]
SPEC_URL = "https://focus.finops.org/docs/specification/v1-0/"
RETRIEVED = "2026-10-19"

BASE = {
    "ChargePeriodStart": "2026-02-01T00:00:00Z", "ChargePeriodEnd": "2026-03-01T00:00:00Z",
    "BilledCost": "1.00", "EffectiveCost": "1.00", "BillingCurrency": "USD",
    "ServiceName": "Amazon Elastic Compute Cloud", "ChargeCategory": "Usage",
}


def write_export(path: Path, rows: list[dict], columns: list[str] | None = None) -> Path:
    """An export file with `columns` (the keys of the first row by default) and `rows`."""
    columns = columns or list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, columns, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_output(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


def row(**changes) -> dict:
    return {**BASE, **changes}


def fails(inputs: list[Path], out: Path) -> str:
    """The message of the exit 2 an import must raise."""
    with pytest.raises(MarginError) as caught:
        import_focus(inputs, out)
    assert caught.value.exit_code == EXIT_USAGE
    return caught.value.message


# ------------------------------------------------------------------ the round trip


@pytest.fixture(scope="module")
def imported(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("round_trip")
    result = margin("import-focus", "--in", str(EXPORT), "--out", str(root / "cloud_focus.csv"), "--json")
    assert result.code == 0, result.stderr
    return {"root": root, "out": root / "cloud_focus.csv", "envelope": result.json}


@pytest.fixture(scope="module")
def staged(imported, tmp_path_factory) -> Path:
    """The output in a temporary warehouse: loaded by `margin.load`, staged through `stg_cloud_focus`.
    The other books are `mini`'s, so this is no close."""
    books = imported["root"] / "books"
    shutil.copytree(MINI_BOOKS, books)
    shutil.copy(imported["out"], books / "cloud_focus.csv")
    warehouse = tmp_path_factory.mktemp("round_trip_wh") / "w.duckdb"
    con = connect(str(warehouse))
    try:
        init_schemas(con)
        load.load_months(con, books, "2026-02", "2026-04", "test")
        load.load_reference(con, MINI_PRICES)
    finally:
        con.close()
    dbt_runner._run(warehouse, MINI_CONFIG, "--select", "stg_cloud_focus")
    return warehouse


def test_the_envelope_reports_the_counts_the_dropped_columns_and_the_months(imported):
    env = imported["envelope"]
    assert (env["status"], env["exit"]) == ("ok", 0)
    assert env["rows_in"] == 16
    assert env["rows_out"] == env["rows_in"] + env["residual_rows"] == 18
    assert env["columns_dropped"] == {"count": 3, "names": EXTRA}
    assert env["months"] == ["2026-02", "2026-03"]


def test_the_output_has_the_sixteen_columns_and_none_of_the_dropped(imported):
    header, rows = read_output(imported["out"])
    assert header == focus_import.OUTPUT_COLUMNS and len(header) == 16
    assert not set(EXTRA) & set(header)
    assert len(rows) == 18


def test_the_row_ids_are_the_file_stem_and_the_position_with_one_residual_id_per_month(imported):
    _, rows = read_output(imported["out"])
    ids = [r["line_id"] for r in rows]
    assert ids == [f"export_{n}" for n in range(1, 9)] + ["export_residual_2026-02"] \
        + [f"export_{n}" for n in range(9, 17)] + ["export_residual_2026-03"]


def test_each_months_four_cost_sums_equal_the_independent_totals_to_the_cent(imported, staged):
    with connect(str(staged), read_only=True) as con:
        got = {
            f"{month:%Y-%m}": dict(zip(COSTS, sums))
            for month, *sums in con.execute(
                "SELECT month, sum(list_cents), sum(contracted_cents), sum(effective_cents), sum(billed_cents) "
                "FROM stg.stg_cloud_focus GROUP BY month ORDER BY month"
            ).fetchall()
        }
    assert got == {m: {c: int(Decimal(v) * 100) for c, v in cols.items()} for m, cols in TOTALS.items()}


def test_the_staged_rows_carry_the_mapped_values(staged):
    with connect(str(staged), read_only=True) as con:
        cur = con.execute("SELECT * FROM stg.stg_cloud_focus WHERE line_id = 'export_1'")
        covered = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        shared = con.execute(
            "SELECT tag_class, tenant, charge_category FROM stg.stg_cloud_focus "
            "WHERE line_id = 'export_residual_2026-02'"
        ).fetchone()
    assert (covered["list_cents"], covered["contracted_cents"], covered["effective_cents"],
            covered["billed_cents"], covered["benefit_cents"]) == (123, 110, 80, 0, 30)
    assert (covered["charge_category"], covered["is_covered"], covered["tag_class"]) == ("Usage", True, "direct")
    assert (covered["tenant"], covered["product"], covered["team"], covered["region_id"]) \
        == ("acct_0101", "api", "growth", "us-east-1")
    assert shared == ("shared", None, "Adjustment")


@pytest.mark.parametrize("month", ["2026-02", "2026-03"])
def test_the_price_pin_passes_on_the_output(staged, month):
    sql = (ROOT / "controls" / "CTL-PRICE-PIN.sql").read_text(encoding="utf-8")
    with connect(str(staged), read_only=True) as con:
        rows = con.execute(sql, {"month": date.fromisoformat(month + "-01")}).fetchall()
    assert rows == []


def test_the_output_is_the_same_bytes_on_a_second_run(tmp_path):
    first, second = tmp_path / "one" / "cloud_focus.csv", tmp_path / "two" / "cloud_focus.csv"
    assert import_focus([EXPORT], first) == import_focus([EXPORT], second)
    assert first.read_bytes() == second.read_bytes()


# ------------------------------------------------------------------ the columns


def test_an_export_with_extra_columns_imports_and_names_them(tmp_path):
    src = write_export(tmp_path / "export.csv", [row(Extra1="a", Extra2="b")])
    summary = import_focus([src], tmp_path / "out.csv")
    assert summary["columns_dropped"] == {"count": 2, "names": ["Extra1", "Extra2"]}
    header, rows = read_output(tmp_path / "out.csv")
    assert "Extra1" not in header and len(rows) == 1


@pytest.mark.parametrize("column", MANDATORY)
def test_a_missing_mandatory_column_is_exit_2_naming_it(tmp_path, column):
    columns = [c for c in BASE if c != column]
    src = write_export(tmp_path / "export.csv", [row()], columns)
    assert column in fails([src], tmp_path / "out.csv")
    assert not (tmp_path / "out.csv").exists()


def test_the_cli_names_a_missing_mandatory_column_in_the_failure_envelope(tmp_path):
    src = write_export(tmp_path / "export.csv", [row()], [c for c in BASE if c != "EffectiveCost"])
    result = margin("import-focus", "--in", str(src), "--out", str(tmp_path / "out.csv"), "--json")
    assert result.code == 2
    assert result.json["status"] == "fail" and "EffectiveCost" in result.json["message"]


def test_missing_optional_columns_are_written_empty(tmp_path):
    src = write_export(tmp_path / "export.csv", [row(), row(EffectiveCost="2.00", BilledCost="2.00")])
    import_focus([src], tmp_path / "out.csv")
    header, rows = read_output(tmp_path / "out.csv")
    assert header == focus_import.OUTPUT_COLUMNS
    assert len(rows) == 2
    for r in rows:
        assert all(r[c] == "" for c in ["ServiceCategory", "ResourceId", "ListCost", "ContractedCost",
                                       "CommitmentDiscountCategory", "CommitmentDiscountType", "RegionId"])
        assert r["Tags"] == "{}"


def test_an_export_with_a_header_and_no_rows_imports_nothing(tmp_path):
    src = write_export(tmp_path / "export.csv", [], list(BASE))
    summary = import_focus([src], tmp_path / "out.csv")
    assert (summary["rows_in"], summary["rows_out"], summary["months"]) == (0, 0, [])
    assert read_output(tmp_path / "out.csv")[1] == []


def test_a_present_optional_cost_with_an_empty_cell_stays_empty(tmp_path):
    src = write_export(tmp_path / "export.csv", [row(ListCost="", ContractedCost="1.239")])
    import_focus([src], tmp_path / "out.csv")
    (only,) = read_output(tmp_path / "out.csv")[1]
    assert (only["ListCost"], only["ContractedCost"]) == ("", "1.24")


def test_a_cost_that_is_not_a_number_names_its_row_and_column(tmp_path):
    src = write_export(tmp_path / "export.csv", [row(), row(BilledCost="n/a")])
    message = fails([src], tmp_path / "out.csv")
    assert "export.csv row 2" in message and "BilledCost" in message


# ------------------------------------------------------------------ tags


def test_tag_keys_are_lowercased_and_values_kept_as_written(tmp_path):
    src = write_export(tmp_path / "export.csv", [
        row(Tags='{"Tenant":"Acct_0101","PRODUCT":"Api","Team":"Growth"}'),
        row(Tags=""),
        row(Tags='{"TENANT":"x","tenant":"x"}'),
    ])
    import_focus([src], tmp_path / "out.csv")
    tags = [r["Tags"] for r in read_output(tmp_path / "out.csv")[1][:3]]
    assert tags == ['{"tenant":"Acct_0101","product":"Api","team":"Growth"}', "{}", '{"tenant":"x"}']


def test_two_keys_that_collide_with_different_values_are_exit_2_naming_the_row_and_the_key(tmp_path):
    src = write_export(tmp_path / "export.csv", [row(Tags="{}"), row(Tags='{"Tenant":"a","tenant":"b"}')])
    message = fails([src], tmp_path / "out.csv")
    assert "export.csv row 2" in message and "'tenant'" in message
    assert not (tmp_path / "out.csv").exists()


@pytest.mark.parametrize("tags", ["not json", '["tenant"]'])
def test_tags_that_are_not_a_json_object_name_the_row(tmp_path, tags):
    src = write_export(tmp_path / "export.csv", [row(Tags=tags)])
    assert "export.csv row 1" in fails([src], tmp_path / "out.csv")


# ------------------------------------------------------------------ rounding and the residual row


def test_costs_round_half_even_to_two_decimals_without_a_negative_zero(tmp_path):
    values = ["0.125", "0.135", "2.5", "-0.001", "1.005", "7"]
    src = write_export(tmp_path / "export.csv", [row(BilledCost=v, EffectiveCost=v) for v in values])
    import_focus([src], tmp_path / "out.csv")
    got = [r["EffectiveCost"] for r in read_output(tmp_path / "out.csv")[1][:6]]
    assert got == ["0.12", "0.14", "2.50", "0.00", "1.00", "7.00"]


def test_a_month_that_does_not_tie_gets_one_complete_adjustment_row(tmp_path):
    cost = {"BilledCost": "0.004999", "EffectiveCost": "0.004999", "ListCost": "0.004999", "ContractedCost": "0.004999"}
    rows = [row(ResourceId=f"res-{n}", Tags='{"Tenant":"a"}', RegionId="us-east-1", **cost) for n in range(3)]
    src = write_export(tmp_path / "Cloud Export.csv", rows)
    summary = import_focus([src], tmp_path / "out.csv")
    assert (summary["rows_in"], summary["rows_out"], summary["residual_rows"]) == (3, 4, 1)
    _, out = read_output(tmp_path / "out.csv")
    assert [r["line_id"] for r in out] == ["cloud_export_1", "cloud_export_2", "cloud_export_3",
                                           "cloud_export_residual_2026-02"]
    assert out[3] == {
        "ChargePeriodStart": "2026-02-01T00:00:00Z", "ChargePeriodEnd": "2026-03-01T00:00:00Z",
        "BilledCost": "0.01", "EffectiveCost": "0.01", "ListCost": "0.01", "ContractedCost": "0.01",
        "BillingCurrency": "USD", "ServiceCategory": "Other", "ServiceName": "Import rounding",
        "ResourceId": "", "RegionId": "", "ChargeCategory": "Adjustment",
        "CommitmentDiscountCategory": "", "CommitmentDiscountType": "",
        "Tags": '{"tenant":"shared"}', "line_id": "cloud_export_residual_2026-02",
    }


def test_the_residual_row_of_december_ends_in_january_of_the_next_year(tmp_path):
    rows = [row(ChargePeriodStart="2026-12-01T00:00:00Z", ChargePeriodEnd="2027-01-01T00:00:00Z",
                BilledCost="0.004", EffectiveCost="0.004")] * 3
    # 3 x 0.004 = 0.012 rounds to 0.01 while each row rounds to 0.00.
    src = write_export(tmp_path / "export.csv", [dict(r) for r in rows])
    import_focus([src], tmp_path / "out.csv")
    residual = read_output(tmp_path / "out.csv")[1][-1]
    assert (residual["ChargePeriodStart"], residual["ChargePeriodEnd"]) == ("2026-12-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert residual["ListCost"] == "" and residual["BilledCost"] == "0.01"


def test_a_month_that_already_ties_gets_no_residual_row(tmp_path):
    src = write_export(tmp_path / "export.csv", [row(), row(BilledCost="2.50", EffectiveCost="2.50")])
    summary = import_focus([src], tmp_path / "out.csv")
    assert (summary["rows_out"], summary["residual_rows"]) == (2, 0)


def test_the_residual_is_negative_when_the_rows_round_up(tmp_path):
    src = write_export(tmp_path / "export.csv", [row(BilledCost="0.006", EffectiveCost="0.006")] * 2)
    import_focus([src], tmp_path / "out.csv")
    out = read_output(tmp_path / "out.csv")[1]
    # 2 x 0.006 = 0.012 -> 0.01, but each row rounds to 0.01: the sum of rows is 0.02.
    assert [r["EffectiveCost"] for r in out] == ["0.01", "0.01", "-0.01"]


# ------------------------------------------------------------------ several files


def test_two_files_make_one_output_in_file_name_order_with_ids_from_their_names(tmp_path):
    feb = write_export(tmp_path / "b-feb.csv", [row(ServiceName="feb")])
    jan = write_export(tmp_path / "A_Jan.csv", [row(ChargePeriodStart="2026-01-01T00:00:00Z",
                                                   ChargePeriodEnd="2026-02-01T00:00:00Z", ServiceName="jan")])
    summary = import_focus([feb, jan], tmp_path / "out.csv")
    assert summary["months"] == ["2026-01", "2026-02"] and summary["rows_in"] == 2
    out = read_output(tmp_path / "out.csv")[1]
    assert [(r["ServiceName"], r["line_id"]) for r in out] == [("jan", "a_jan_1"), ("feb", "b_feb_1")]


def test_a_directory_means_its_csv_files_in_sorted_order(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    write_export(exports / "2.csv", [row(ChargePeriodStart="2026-03-01T00:00:00Z", ChargePeriodEnd="2026-04-01T00:00:00Z")])
    write_export(exports / "1.csv", [row()])
    (exports / "notes.txt").write_text("not an export", encoding="utf-8")
    summary = import_focus([exports], tmp_path / "out.csv")
    assert summary["months"] == ["2026-02", "2026-03"]
    assert [r["line_id"] for r in read_output(tmp_path / "out.csv")[1]] == ["1_1", "2_1"]


def test_ids_do_not_depend_on_which_other_files_were_given(tmp_path):
    feb = write_export(tmp_path / "feb.csv", [row()])
    mar = write_export(tmp_path / "mar.csv", [row(ChargePeriodStart="2026-03-01T00:00:00Z", ChargePeriodEnd="2026-04-01T00:00:00Z")])
    import_focus([feb], tmp_path / "alone.csv")
    import_focus([mar, feb], tmp_path / "both.csv")
    assert read_output(tmp_path / "alone.csv")[1][0]["line_id"] == read_output(tmp_path / "both.csv")[1][0]["line_id"] == "feb_1"


def test_a_charge_month_in_two_files_is_exit_2_naming_both(tmp_path):
    one = write_export(tmp_path / "first.csv", [row()])
    two = write_export(tmp_path / "second.csv", [row(ServiceName="again")])
    message = fails([one, two], tmp_path / "out.csv")
    assert "2026-02" in message and "first.csv" in message and "second.csv" in message
    assert not (tmp_path / "out.csv").exists()


def test_two_files_whose_names_make_the_same_id_prefix_are_exit_2(tmp_path):
    one = write_export(tmp_path / "a-b.csv", [row()])
    two = write_export(tmp_path / "a_b.csv", [row(ChargePeriodStart="2026-03-01T00:00:00Z", ChargePeriodEnd="2026-04-01T00:00:00Z")])
    message = fails([one, two], tmp_path / "out.csv")
    assert "a-b.csv" in message and "a_b.csv" in message


def test_a_missing_input_and_an_empty_directory_are_exit_2(tmp_path):
    assert "no such export" in fails([tmp_path / "absent.csv"], tmp_path / "out.csv")
    (tmp_path / "empty").mkdir()
    assert "no *.csv" in fails([tmp_path / "empty"], tmp_path / "out.csv")


def test_the_output_is_written_whole_and_a_failure_leaves_the_old_file(tmp_path):
    out = tmp_path / "cloud_focus.csv"
    out.write_text("previous", encoding="utf-8")
    import_focus([EXPORT], out)
    assert out.read_text(encoding="utf-8").startswith("ChargePeriodStart,")
    good = out.read_bytes()
    bad = write_export(tmp_path / "bad.csv", [row(BilledCost="x")])
    fails([bad], out)
    assert out.read_bytes() == good
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


# ------------------------------------------------------------------ the pin


def test_the_parser_and_the_reference_file_carry_the_same_specification_pin():
    source = (ROOT / "src" / "margin" / "focus_import.py").read_text(encoding="utf-8")
    reference = (ROOT / "data" / "reference" / "focus_columns.md").read_text(encoding="utf-8")
    assert SPEC_URL in source.split('"""')[0] and RETRIEVED in source.split('"""')[0]
    assert SPEC_URL in reference and RETRIEVED in reference
    for column in focus_import.OUTPUT_COLUMNS:
        assert f"`{column}`" in reference
    assert re.search(r"\*\*EffectiveCost\*\*", reference)


# ------------------------------------------------------------------ speed


def test_the_fixture_imports_within_two_seconds(tmp_path):
    started = time.perf_counter()
    import_focus([EXPORT], tmp_path / "out.csv")
    assert time.perf_counter() - started < 2


def synthesise(path: Path, rows: int, seed: int = 7) -> None:
    rng = random.Random(seed)
    services = [("Compute", "Amazon Elastic Compute Cloud"), ("Storage", "Amazon Simple Storage Service"),
                ("Networking", "Amazon API Gateway"), ("Databases", "Amazon Relational Database Service")]
    columns = [*BASE, "ServiceCategory", "ResourceId", "Tags", "ListCost", "ContractedCost",
               "CommitmentDiscountCategory", "CommitmentDiscountType", "RegionId", "ChargeDescription"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        for n in range(rows):
            month = "2026-02" if n < rows // 2 else "2026-03"
            nxt = "2026-03" if month == "2026-02" else "2026-04"
            category, service = services[n % len(services)]
            list_cost = Decimal(rng.randrange(1, 10_000_000)) / Decimal(1_000_000)
            effective = (list_cost * Decimal("0.8")).quantize(Decimal("0.000001"))
            covered = n % 5 == 0
            writer.writerow([
                f"{month}-01T00:00:00Z", f"{nxt}-01T00:00:00Z", "0.000000" if covered else effective, effective,
                "USD", service, "Usage", category, f"res-{n % 500:04d}",
                json.dumps({"Tenant": f"acct_{n % 40:04d}", "Team": "t"}), list_cost, (list_cost * Decimal("0.9")).quantize(Decimal("0.000001")),
                "Spend" if covered else "", "Compute Savings Plan" if covered else "", "us-east-1", "synthetic",
            ])


@pytest.mark.slow
def test_a_hundred_thousand_row_export_imports_within_sixty_seconds(tmp_path):
    src = tmp_path / "big.csv"
    synthesise(src, 100_000)
    started = time.perf_counter()
    summary = import_focus([src], tmp_path / "out.csv")
    elapsed = time.perf_counter() - started
    assert summary["rows_in"] == 100_000 and summary["months"] == ["2026-02", "2026-03"]
    assert summary["rows_out"] == 100_000 + summary["residual_rows"]
    assert elapsed < 60
