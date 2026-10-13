"""The loader (plan §6.2 step 2, §7.1): books into `raw.*`, every column VARCHAR.

`_row_hash` and `_month` are computed once, here, in SQL. A month already
loaded has its row hashes verified rather than reloaded, so loading is idempotent.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from .books import months_between, read_books_id
from .errors import EXIT_CONTROL, EXIT_PRECONDITION, EXIT_USAGE, MarginError
from .warehouse import month_date

# table -> (csv file, SQL expression for the month of a row; None = not monthly)
TABLES = {
    "accounts": ("accounts.csv", None),
    "invoices": ("invoices.csv", "service_period"),
    "usage_monthly": ("usage_monthly.csv", "month"),
    "gl": ("gl.csv", "gl_period"),
}


def _read_sql(csv_path: Path) -> str:
    return f"read_csv('{csv_path}', all_varchar=true, header=true)"


def _columns(con: duckdb.DuckDBPyConnection, csv_path: Path) -> list[str]:
    rows = con.execute(f"DESCRIBE SELECT * FROM {_read_sql(csv_path)}").fetchall()
    return [r[0] for r in rows]


def books_have_rows(books: Path, month: str) -> bool:
    """True if the books carry any invoice row for `month` (EC-03); no writes."""
    with duckdb.connect(":memory:") as con:
        n = con.execute(
            f"SELECT count(*) FROM {_read_sql(books / 'invoices.csv')} WHERE service_period = ?",
            [month],
        ).fetchone()[0]
    return n > 0


def check_books_identity(con: duckdb.DuckDBPyConnection, books: Path) -> str:
    """Record `state.books.books_id` at first load; a different books dir exits 2."""
    books_id = read_books_id(books)
    row = con.execute("SELECT books_id FROM state.books").fetchone()
    if row is None:
        con.execute("INSERT INTO state.books VALUES (?)", [books_id])
    elif row[0] != books_id:
        raise MarginError(
            EXIT_USAGE, "warehouse holds different books: use --warehouse or delete it"
        )
    return books_id


def _stage(con: duckdb.DuckDBPyConnection, books: Path, table: str, run_id: str) -> list[str]:
    """Stage one books file into a temp table with `_row_hash`, `_month`, `_load_id`."""
    csv_name, month_col = TABLES[table]
    csv_path = (books / csv_name).resolve()
    if not csv_path.is_file():
        raise MarginError(EXIT_PRECONDITION, f"no {csv_name} in {books}: run margin gen first")
    cols = _columns(con, csv_path)
    fields = " || chr(31) || ".join(f'coalesce("{c}", chr(0))' for c in cols)
    month_sql = f"CAST(\"{month_col}\" || '-01' AS DATE)" if month_col else "CAST(NULL AS DATE)"
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE stage_{table} AS
        SELECT {", ".join(f'"{c}"' for c in cols)},
               md5('{csv_name}' || chr(31) || CAST(_ordinal AS VARCHAR) || chr(31) || {fields})
                   AS _row_hash,
               {month_sql} AS _month,
               '{run_id}' AS _load_id
        FROM (SELECT *, row_number() OVER () AS _ordinal FROM {_read_sql(csv_path)})
        """
    )
    return cols


def load_months(
    con: duckdb.DuckDBPyConnection, books: Path, first: str, last: str, run_id: str
) -> None:
    """Load every month in `first..last` that is absent; verify the ones present."""
    for table, (_, month_col) in TABLES.items():
        cols = _stage(con, books, table, run_id)
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'raw' AND table_name = ?",
            [table],
        ).fetchone()[0]
        if not exists:
            con.execute(f"CREATE TABLE raw.{table} AS SELECT * FROM stage_{table} WHERE false")
        groups = [None] if month_col is None else [month_date(m) for m in months_between(first, last)]
        for group in groups:
            cond = "_month IS NULL" if group is None else f"_month = DATE '{group}'"
            have = con.execute(
                f"SELECT list(_row_hash ORDER BY _row_hash) FROM raw.{table} WHERE {cond}"
            ).fetchone()[0]
            new = con.execute(
                f"SELECT list(_row_hash ORDER BY _row_hash) FROM stage_{table} WHERE {cond}"
            ).fetchone()[0]
            if have is None and new is not None:
                col_list = ", ".join(f'"{c}"' for c in cols + ["_row_hash", "_month", "_load_id"])
                con.execute(
                    f"INSERT INTO raw.{table} SELECT {col_list} FROM stage_{table} WHERE {cond}"
                )
            elif have is not None and have != new:
                label = "books" if group is None else f"month {group[:7]}"
                raise MarginError(
                    EXIT_CONTROL, f"raw.{table}: {label} changed since it was loaded"
                )
        con.execute(f"DROP TABLE stage_{table}")
