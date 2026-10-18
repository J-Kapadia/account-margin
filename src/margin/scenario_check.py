"""`scenario --check`: the token-price scenario against a hand-worked reference.

Generates the `scenario` preset into a temporary directory, closes it in a temporary warehouse
(`MARGIN_WAREHOUSE` when set) with the fixture config through the normal loader, runs the scenario
on it, and compares the series, the computed change and each fixture account's recovery quarter and
margin path with the files of the reference directory. Any difference fails CHK-SCENARIO.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import close as close_mod
from . import gen as gen_mod
from . import gen_config, gen_plan, scenario, scenario_run, signoff
from .errors import EXIT_CONTROL, EXIT_PRECONDITION, MarginError
from .lock import warehouse_lock
from .paths import DEFAULT_PRICES, ROOT, Paths
from .warehouse import connect, month_date

CHECK_ID = "CHK-SCENARIO"
MONTH = "2026-03"
REFERENCE = ROOT / "tests" / "fixtures" / "scenario"
FIXTURE_CONFIG = ROOT / "tests" / "fixtures" / "config"
FIXTURE_SIGNOFF = ROOT / "tests" / "fixtures" / "signoff"
DEFAULT_WAREHOUSE = Path("warehouse/margin.duckdb")  # what path resolution gives when none was named
EXPECTED_FILE = "expected.csv"
SUMMARY_FILE = "summary.json"
_QUARTER_ENDS = {k: scenario.quarter_of(k) for k in scenario.QUARTER_END_K}


def _fail(detail: list[str]) -> MarginError:
    return MarginError(
        EXIT_CONTROL, f"{CHECK_ID} failed: {len(detail)} difference(s) from the reference",
        controls=[{"id": CHECK_ID, "status": "fail", "violating_rows": len(detail)}],
        failed_controls=[{"id": CHECK_ID, "violating_rows": len(detail), "detail": detail}],
    )


def _read_reference(reference: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(expected value of every scenario number ID, summary figures) from the reference directory."""
    try:
        with (reference / EXPECTED_FILE).open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        summary = json.loads((reference / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise MarginError(EXIT_PRECONDITION, f"cannot read the reference in {reference}: {err}") from None
    expected: dict[str, str] = {}
    try:
        for row in rows:
            prefix = f"scenario/{MONTH}/{row['case']}/{row['quarter']}/{row['account_id']}"
            expected[f"{prefix}/inference_cost"] = row["inference_cost_cents"]
            expected[f"{prefix}/contribution_profit"] = row["contribution_profit_cents"]
            expected[f"{prefix}/margin_pct"] = row["margin_pct"]
            expected[f"{prefix}/above_floor"] = row["above_floor"]
            expected[f"scenario/{MONTH}/{row['case']}/summary/{row['account_id']}/recovery_quarter"] = (
                row["recovery_quarter"]
            )
    except (KeyError, TypeError) as err:
        raise MarginError(EXIT_PRECONDITION, f"{EXPECTED_FILE} in {reference} lacks column {err}") from None
    return expected, summary


@contextmanager
def _signoff_dir() -> Iterator[None]:
    """`MARGIN_SIGNOFF_DIR` as given, else the committed fixture sign-off, for the length of the check."""
    previous = os.environ.get(signoff.SIGNOFF_DIR_ENV)
    if not previous:
        os.environ[signoff.SIGNOFF_DIR_ENV] = str(FIXTURE_SIGNOFF)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(signoff.SIGNOFF_DIR_ENV, None)
        else:
            os.environ[signoff.SIGNOFF_DIR_ENV] = previous


def _generate(books: Path, truth: Path, prices: Path) -> None:
    preset = gen_plan.PRESETS["scenario"]
    gen_mod.generate(
        books=books, truth=truth, config=gen_config.load_config(FIXTURE_CONFIG),
        prices=gen_config.load_prices(prices), seed=gen_mod.DEFAULT_SEED, preset="scenario",
        n_accounts=preset["accounts"], n_months=preset["months"], first_month=preset["first_month"],
    )


def _actual(warehouse: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(values read from `scenario.results`, values read from the `scenario/` records), by number ID."""
    con = connect(warehouse, read_only=True)
    try:
        rows = con.execute(
            'SELECT "case", k, account_id, inference_cost_cents, contribution_profit_cents, margin_pct, '
            "below_floor, recovery_quarter FROM scenario.results WHERE month = ? AND k IN (3, 6, 9, 12)",
            [month_date(MONTH)],
        ).fetchall()
        stored = {
            number_id: text for number_id, text in con.execute(
                "SELECT number_id, value_text FROM prov.records WHERE month = ? AND number_id LIKE 'scenario/%'",
                [month_date(MONTH)],
            ).fetchall()
        }
    finally:
        con.close()
    results: dict[str, str] = {}
    for case, k, account, inference, profit, margin, below, recovery in rows:
        prefix = f"scenario/{MONTH}/{case}/{_QUARTER_ENDS[k]}/{account}"
        results[f"{prefix}/inference_cost"] = str(inference)
        results[f"{prefix}/contribution_profit"] = str(profit)
        results[f"{prefix}/margin_pct"] = "n/a" if margin is None else format(margin, "f")
        results[f"{prefix}/above_floor"] = "false" if below else "true"
        results[f"scenario/{MONTH}/{case}/summary/{account}/recovery_quarter"] = recovery
    return results, stored


def _differences(
    expected: dict[str, str], results: dict[str, str], stored: dict[str, str],
    summary: dict[str, str], run: dict, series_rows: list[dict],
) -> list[str]:
    """One line per scenario number whose computed value differs from the reference, led by its ID.
    The records hold the cents as text too, so both stores are compared as text."""
    detail: list[str] = []
    for number_id in sorted(expected):
        want, got, recorded = expected[number_id], results.get(number_id), stored.get(number_id)
        if got != want or recorded != want:
            seen = f"results {got}" + ("" if recorded == got else f", record {recorded}")
            detail.append(f"{number_id}: reference {want}, {seen}")
    for number_id in sorted(set(results) - set(expected)):
        detail.append(f"{number_id}: not in the reference")
    blend_first = format(scenario.blend(series_rows, scenario.PERIODS[0]), "f")
    blend_last = format(scenario.blend(series_rows, scenario.PERIODS[-1]), "f")
    figures = {
        "series/blend_first": (summary.get("blend_first"), blend_first),
        "series/blend_last": (summary.get("blend_last"), blend_last),
        f"scenario/{MONTH}/summary/computed_change": (summary.get("computed_change"), run["computed_change"]),
        f"scenario/{MONTH}/summary/monthly_rate": (summary.get("monthly_rate"), run["monthly_rate"]),
    }
    for number_id, (want, got) in figures.items():
        recorded = stored.get(number_id)
        if got != want or recorded not in (None, want):
            detail.append(f"{number_id}: reference {want}, computed {got}"
                          + ("" if recorded in (None, got) else f", record {recorded}"))
    return detail


def check(paths: Paths, reference: Path = REFERENCE) -> dict:
    """Run the check; return the envelope fields, or raise exit 3 naming the numbers that differ.

    `paths.warehouse` is used when it is not the default (`--warehouse` or `MARGIN_WAREHOUSE`), else
    the warehouse lives in the temporary directory. The books, the answer key and the config are
    always the preset's own: the reference is worked from them.
    """
    series = scenario_run.series_path()
    try:
        series_rows = scenario.read_series(series)
    except OSError as err:
        raise _fail([f"{series.name}: cannot be read: {err.strerror or err}"]) from None
    problems = scenario.validate_series(series_rows)
    if problems:
        raise _fail(problems)
    expected, summary = _read_reference(reference)

    with tempfile.TemporaryDirectory(prefix="margin-scenario-check-") as tmp_name, _signoff_dir():
        tmp = Path(tmp_name)
        warehouse = tmp / "check.duckdb" if paths.warehouse == DEFAULT_WAREHOUSE else paths.warehouse
        run_paths = Paths(
            books=tmp / "books", truth=tmp / "truth", warehouse=warehouse, config=FIXTURE_CONFIG,
            fixture="scenario", truth_explicit=True, prices=ROOT / DEFAULT_PRICES,
            reference=paths.reference,
        )
        _generate(run_paths.books, run_paths.truth, run_paths.prices)
        with warehouse_lock(run_paths.lock, exclusive=True):
            close_mod.close_month(run_paths, MONTH)
            run = scenario_run.run_month(run_paths, MONTH, signoff.verify(FIXTURE_CONFIG, month=MONTH))
            results, stored = _actual(warehouse)

    detail = _differences(expected, results, stored, summary, run, series_rows)
    if detail:
        raise _fail(detail)
    return {
        "accounts": run["accounts"], "computed_change": run["computed_change"],
        "checks": [{"id": CHECK_ID, "status": "pass", "violating_rows": 0}],
    }
