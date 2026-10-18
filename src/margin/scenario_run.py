"""`scenario --month M`: the token-price scenario for one closed month.

The only writer of `scenario.inputs`, `scenario.results` and the `scenario/` provenance records.
Python scales the base month's inference cost by the list-price trend (exact `Decimal` arithmetic
in `scenario.py`), hands the cents to the dbt model through `scenario.inputs`, and reads the
profit, margin and floor test back from `scenario.results`. The caller holds the exclusive lock.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path

import duckdb

from . import dbt_runner, prov, scenario
from .errors import EXIT_PRECONDITION, EXIT_PROVENANCE, MarginError
from .load import REFERENCE_DIR
from .paths import Paths
from .signoff import Signoff
from .warehouse import connect, init_schemas, month_date

SERIES_FILE = REFERENCE_DIR / "list_price_series.csv"
SERIES_ENV = "MARGIN_LIST_PRICE_SERIES"
PERSISTENCE_PCT = {"full": "100", "half": "50", "flat": "0"}
QUARTERS = tuple(f"Q{q}" for q in range(1, 5))
DBT_STEP = "dbt:scn_contribution"
PYTHON_STEP = "margin.scenario"
_COMPONENTS = prov.LEAF_COMPONENTS


def _plain(value: Decimal) -> str:
    """A decimal as text without an exponent, at the digits it was computed to."""
    return format(value, "f")


def _population(con: duckdb.DuckDBPyConnection, month: str) -> list[tuple[str, int]]:
    """(account, base inference cents) of every account flagged `firm` or `soft` in `month`."""
    return [
        (account, int(cents)) for account, cents in con.execute(
            "SELECT p.account_id, p.inference_cost_cents FROM mart.fct_contribution_profit p "
            "JOIN mart.fct_flags f ON f.month = p.month AND f.account_id = p.account_id "
            "WHERE p.month = ? AND f.flag IN ('firm', 'soft') ORDER BY p.account_id",
            [month_date(month)],
        ).fetchall()
    ]


def series_path() -> Path:
    """The list-price series file: `MARGIN_LIST_PRICE_SERIES`, else the committed one."""
    return Path(os.environ.get(SERIES_ENV) or SERIES_FILE)


def _trend() -> tuple[Decimal, Decimal]:
    """(monthly rate, computed change) of the list-price series."""
    path = series_path()
    rows = scenario.read_series(path)
    problems = scenario.validate_series(rows)
    if problems:
        raise MarginError(EXIT_PRECONDITION, f"{path.name} is not usable: {problems[0]}")
    return scenario.monthly_rate(rows), scenario.computed_change(rows)


def _write_inputs(
    con: duckdb.DuckDBPyConnection, month: str, population: list[tuple[str, int]], rate: Decimal,
) -> None:
    rows = [
        (month_date(month), case, k, account, scenario.scaled_cents(base, scenario.factor(p, rate, k)))
        for account, base in population
        for case, p in scenario.CASES.items()
        for k in range(1, scenario.QUARTER_END_K[-1] + 1)
    ]
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM scenario.inputs WHERE month = ?", [month_date(month)])
        if rows:  # a month with no flagged account has nothing to scale
            con.executemany("INSERT INTO scenario.inputs VALUES (?, ?, ?, ?, ?)", rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _children(con: duckdb.DuckDBPyConnection, ids: list[str]) -> dict[str, dict]:
    found = {}
    for number_id in ids:
        rec = prov.fetch_record(con, number_id)
        if rec is None:
            raise MarginError(EXIT_PROVENANCE, f"provenance incomplete: no record for {number_id}")
        found[number_id] = rec
    return found


def _records(
    month: str, base: dict, known: dict[str, dict], results: list[tuple], rate: Decimal,
    change: Decimal,
) -> list[dict]:
    """The `scenario/` records of `month`. `results` holds (case, k, account, inference cents,
    profit cents, margin pct, below floor, recovery quarter) for every input row; `known` the
    `cp/` and `ref/` records they hang on."""
    out: dict[str, dict] = {}
    refs = [f"ref/list_price_series/{tier}" for tier in scenario.TIERS]

    def add(number_id: str, children: list[str], **value) -> dict:
        out[number_id] = prov.composite_record(
            base, number_id, [out.get(c) or known[c] for c in children], **value
        )
        return out[number_id]

    for case, k, account, inference, profit, margin, below, _recovery in results:
        if k not in scenario.QUARTER_END_K:
            continue
        cp = [f"cp/{month}/{account}/{c}" for c in _COMPONENTS] + refs
        prefix = f"scenario/{month}/{case}/{scenario.quarter_of(k)}/{account}"
        add(f"{prefix}/inference_cost", cp, value_cents=inference, unit="cents", step=DBT_STEP)
        add(f"{prefix}/contribution_profit", cp, value_cents=profit, unit="cents", step=DBT_STEP)
        add(f"{prefix}/margin_pct", cp, unit="percent", step=DBT_STEP,
            value_text="n/a" if margin is None else _plain(margin))
        add(f"{prefix}/above_floor", cp, unit="boolean", step=DBT_STEP,
            value_text="false" if below else "true")
    recovered: dict[str, dict[str, str]] = {case: {} for case in scenario.CASES}
    for case, _k, account, *_rest, recovery in results:
        recovered[case][account] = recovery
    for case in scenario.CASES:
        for account, recovery in recovered[case].items():
            quarters = [f"scenario/{month}/{case}/{q}/{account}/contribution_profit" for q in QUARTERS]
            add(f"scenario/{month}/{case}/summary/{account}/recovery_quarter", quarters,
                value_text=recovery, unit="quarter", step=DBT_STEP)
    add(f"scenario/{month}/summary/monthly_rate", refs, value_text=_plain(rate), unit="ratio",
        step=PYTHON_STEP)
    add(f"scenario/{month}/summary/computed_change", refs, value_text=_plain(change), unit="ratio",
        step=PYTHON_STEP)
    for case in scenario.CASES:
        summary = f"scenario/{month}/{case}/summary"
        add(f"{summary}/persistence_pct", refs, value_text=PERSISTENCE_PCT[case], unit="percent",
            step=PYTHON_STEP)
        recovery_ids = [f"{summary}/{account}/recovery_quarter" for account in sorted(recovered[case])]
        for i, quarter in enumerate(QUARTERS):
            count = sum(1 for r in recovered[case].values() if r in QUARTERS[: i + 1])
            add(f"{summary}/accounts_recovered_{quarter}", recovery_ids, value_cents=count,
                unit="accounts", step=PYTHON_STEP)
    return list(out.values())


def _store(con: duckdb.DuckDBPyConnection, month: str, records: list[dict]) -> None:
    """Replace the month's `scenario/` records in one transaction and re-derive each digest from
    what was stored; a mismatch rolls everything back and exits 6."""
    by_id = {r["number_id"]: r for r in records}

    def get_child(number_id: str) -> dict | None:
        return by_id.get(number_id) or prov.fetch_record(con, number_id)

    con.execute("BEGIN")
    try:
        con.execute(
            "DELETE FROM prov.records WHERE month = ? AND number_id LIKE 'scenario/%'", [month_date(month)]
        )
        prov.write_records(con, records)
        for rec in records:
            stored = prov.fetch_record(con, rec["number_id"])
            digest, _, count = prov.rederive(con, stored, get_child)
            if digest != stored["input_digest"] or count != stored["input_row_count"]:
                raise MarginError(
                    EXIT_PROVENANCE,
                    f"digest mismatch for {rec['number_id']}: stored {stored['input_digest']}, "
                    f"derived {digest}", month=month,
                )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def run_month(paths: Paths, month: str, sign: Signoff) -> dict:
    """Run the scenario for `month` (closed or locked; the caller has checked) and return the
    envelope fields: month, accounts, cases, records, monthly_rate, computed_change."""
    rate, change = _trend()
    refs = [f"ref/list_price_series/{tier}" for tier in scenario.TIERS]
    con = connect(paths.warehouse)
    try:
        init_schemas(con)
        population = _population(con, month)
        known = _children(con, refs + [
            f"cp/{month}/{account}/{c}" for account, _ in population for c in _COMPONENTS
        ])
        _write_inputs(con, month, population, rate)
    finally:
        con.close()  # dbt needs the file: margin holds no connection while it runs

    dbt_runner.build_scenario(paths.warehouse, paths.config)

    con = connect(paths.warehouse)
    try:
        results = con.execute(
            'SELECT "case", k, account_id, inference_cost_cents, contribution_profit_cents, '
            "margin_pct, below_floor, recovery_quarter FROM scenario.results WHERE month = ? "
            'ORDER BY "case", account_id, k', [month_date(month)],
        ).fetchall()
        base = prov.scenario_base(month, run_id=uuid.uuid4().hex, signoff=sign)
        records = _records(month, base, known, results, rate, change)
        _store(con, month, records)
    finally:
        con.close()
    return {
        "month": month, "accounts": len(population), "cases": list(scenario.CASES),
        "records": len(records), "monthly_rate": _plain(rate), "computed_change": _plain(change),
    }
