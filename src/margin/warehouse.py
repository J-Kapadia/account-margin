"""The DuckDB warehouse: connection helpers and the non-dbt schemas."""

from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMAS = ("raw", "stg", "mart", "ctl", "prov", "state")

DDL = """
CREATE TABLE IF NOT EXISTS ctl.control_results (
    run_id VARCHAR, month DATE, control_id VARCHAR, status VARCHAR,
    violating_rows INTEGER, detail VARCHAR
);
CREATE TABLE IF NOT EXISTS prov.records (
    number_id VARCHAR PRIMARY KEY, month DATE, value_cents BIGINT, value_text VARCHAR,
    unit VARCHAR, step VARCHAR, git_sha VARCHAR, producer_kind VARCHAR, producer_id VARCHAR,
    input_digest VARCHAR, input_row_count INTEGER, source_query VARCHAR,
    checks_passed VARCHAR[], signer VARCHAR, signoff_ref VARCHAR, run_id VARCHAR
);
CREATE TABLE IF NOT EXISTS state.close_state (
    month DATE PRIMARY KEY, status VARCHAR, input_digest VARCHAR, result_hash VARCHAR,
    closed_run_id VARCHAR, restated_from VARCHAR
);
CREATE TABLE IF NOT EXISTS state.books (books_id VARCHAR);
"""


def connect(path: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def init_schemas(con: duckdb.DuckDBPyConnection) -> None:
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    con.execute(DDL)


def month_date(month: str) -> str:
    """`2026-03` -> `2026-03-01`, the DATE form used in the warehouse."""
    return f"{month}-01"
