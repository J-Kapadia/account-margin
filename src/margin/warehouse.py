"""The DuckDB warehouse: connection helpers and the non-dbt schemas."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import duckdb

SCHEMAS = ("raw", "stg", "mart", "ctl", "prov", "state", "scenario")

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
CREATE TABLE IF NOT EXISTS state.model_sql (model VARCHAR PRIMARY KEY, sql VARCHAR);
CREATE TABLE IF NOT EXISTS scenario.inputs (
    month DATE, "case" VARCHAR, k INTEGER, account_id VARCHAR, inference_cents BIGINT
);
"""


# DuckDB would take 80% of the machine's memory per connection, and several processes open
# connections at once (a close, its dbt subprocess, tests running in parallel): every
# connection the product opens is capped instead.
MEMORY_LIMIT_VAR = "MARGIN_DUCKDB_MEMORY_LIMIT"
DEFAULT_MEMORY_LIMIT = "2GB"
# Likewise DuckDB would use every core per connection: several processes each running a
# query at once would make the load several times the core count.
THREADS_VAR = "MARGIN_DUCKDB_THREADS"
DEFAULT_THREADS = 2


def memory_limit() -> str:
    """The cap on every connection: `MARGIN_DUCKDB_MEMORY_LIMIT`, default 2GB."""
    return os.environ.get(MEMORY_LIMIT_VAR) or DEFAULT_MEMORY_LIMIT


def threads() -> int:
    """The thread cap on every connection: `MARGIN_DUCKDB_THREADS`, default 2."""
    raw = os.environ.get(THREADS_VAR) or str(DEFAULT_THREADS)
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{THREADS_VAR} must be a positive integer, not {raw!r}")
    return int(raw)


def temp_directory(path: Path | None) -> Path:
    """Where a connection spills to disk: beside the warehouse file, or for an in-memory
    database under the system temp directory, one per process."""
    if path is None:
        return Path(tempfile.gettempdir()) / f"margin-duckdb-{os.getpid()}.tmp"
    return path.parent / f"{path.name}.tmp"


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the warehouse at `path`, or an in-memory database when `path` is None, capped.

    The caps and the spill directory are applied with SET after opening rather than passed as
    connection config: DuckDB shares one instance per file within a process and refuses a
    second connection opened with a different config."""
    if path is None:
        con = duckdb.connect(":memory:")
    else:
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path), read_only=read_only)
    con.execute(f"SET memory_limit = {_quoted(memory_limit())}")
    con.execute(f"SET threads = {threads()}")
    con.execute(f"SET temp_directory = {_quoted(str(temp_directory(path)))}")
    return con


def init_schemas(con: duckdb.DuckDBPyConnection) -> None:
    for schema in SCHEMAS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    con.execute(DDL)


def month_date(month: str) -> str:
    """`2026-03` -> `2026-03-01`, the DATE form used in the warehouse."""
    return f"{month}-01"
