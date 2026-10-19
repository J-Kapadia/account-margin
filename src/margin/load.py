"""The loader: books into `raw.*`, every column VARCHAR.

`_row_hash` and `_month` are computed once, here, in SQL. A month already
loaded has its row hashes verified rather than reloaded, so loading is idempotent.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from .books import months_between, read_books_id
from .gen_config import ALLOCATION_FILE, POLICY_FILE, sha256_file
from .errors import EXIT_CONTROL, EXIT_PRECONDITION, EXIT_USAGE, MarginError
from .paths import ROOT, default_reference
from .warehouse import connect, month_date

# table -> (csv file, SQL expression for the month of a row as YYYY-MM; None = not monthly).
# The month of a FOCUS row is that of ChargePeriodStart; a ticket's is that of its closed_date,
# and a ticket still open has none.
TABLES = {
    "accounts": ("accounts.csv", None),
    "invoices": ("invoices.csv", '"service_period"'),
    "usage_monthly": ("usage_monthly.csv", '"month"'),
    "provider_invoices": ("provider_invoices.csv", '"month"'),
    "cloud_focus": ("cloud_focus.csv", 'substr("ChargePeriodStart", 1, 7)'),
    "support_tickets": ("support_tickets.csv", 'substr(nullif("closed_date", \'\'), 1, 7)'),
    "gl": ("gl.csv", '"gl_period"'),
}
# Books that may be absent (the `small` fixture has no costs): an empty table with these
# columns stands in, so the models read the same tables whatever the books hold.
OPTIONAL_COLUMNS = {
    "provider_invoices": ["provider", "model", "month", "tokens", "amount_cents"],
    "cloud_focus": [
        "ChargePeriodStart", "ChargePeriodEnd", "BilledCost", "EffectiveCost", "BillingCurrency",
        "ServiceName", "ServiceCategory", "ResourceId", "Tags", "line_id",
        "ListCost", "ContractedCost", "ChargeCategory", "CommitmentDiscountCategory",
        "CommitmentDiscountType", "RegionId",
    ],
    "support_tickets": ["ticket_id", "account_id", "opened_date", "closed_date", "handle_minutes", "tier"],
}
# Columns a file may leave out whole: a book written before they existed. They are NULL in
# `raw.*` when absent, and `_row_hash` covers only the columns the file has, so an older book
# keeps the hashes it always had.
ABSENT_OK = {
    "cloud_focus": [
        "ListCost", "ContractedCost", "ChargeCategory", "CommitmentDiscountCategory",
        "CommitmentDiscountType", "RegionId",
    ],
}
REFERENCE_DIR = ROOT / "data" / "reference"
PRICES_CSV = "model_prices.csv"
PRICE_COLUMNS = ["provider", "model", "input_per_mtok", "output_per_mtok", "source_url", "retrieved_on"]


def _read_sql(csv_path: Path) -> str:
    return f"read_csv('{csv_path}', all_varchar=true, header=true)"


def _columns(con: duckdb.DuckDBPyConnection, csv_path: Path) -> list[str]:
    rows = con.execute(f"DESCRIBE SELECT * FROM {_read_sql(csv_path)}").fetchall()
    return [r[0] for r in rows]


def books_have_rows(books: Path, month: str) -> bool:
    """True if the books carry any invoice row for `month`; no writes."""
    with connect() as con:
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
    csv_name, month_expr = TABLES[table]
    csv_path = (books / csv_name).resolve()
    if not csv_path.is_file():
        if table not in OPTIONAL_COLUMNS:
            raise MarginError(EXIT_PRECONDITION, f"no {csv_name} in {books}: run margin gen first")
        cols = OPTIONAL_COLUMNS[table]
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE stage_{table} AS SELECT "
            + ", ".join(f'CAST(NULL AS VARCHAR) AS "{c}"' for c in cols)
            + ", CAST(NULL AS VARCHAR) AS _row_hash, CAST(NULL AS DATE) AS _month, "
            "CAST(NULL AS VARCHAR) AS _load_id WHERE false"
        )
        return cols
    cols = _columns(con, csv_path)
    fields = " || chr(31) || ".join(f'coalesce("{c}", chr(0))' for c in cols)
    padding = [c for c in ABSENT_OK.get(table, []) if c not in cols]
    # A present optional column keeps an empty cell as empty text, so a control can tell it from
    # a column the file does not have (NULL); the hash below still reads the cell as parsed.
    present = {c for c in ABSENT_OK.get(table, []) if c in cols}
    select_cols = ", ".join(f'coalesce("{c}", \'\') AS "{c}"' if c in present else f'"{c}"' for c in cols)
    month_sql = f"CAST(({month_expr}) || '-01' AS DATE)" if month_expr else "CAST(NULL AS DATE)"
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE stage_{table} AS
        SELECT {select_cols},
               {"".join(f'CAST(NULL AS VARCHAR) AS "{c}", ' for c in padding)}
               md5('{csv_name}' || chr(31) || CAST(_ordinal AS VARCHAR) || chr(31) || {fields})
                   AS _row_hash,
               {month_sql} AS _month,
               '{run_id}' AS _load_id
        FROM (SELECT *, row_number() OVER () AS _ordinal FROM {_read_sql(csv_path)})
        """
    )
    return cols + padding


def load_months(
    con: duckdb.DuckDBPyConnection, books: Path, first: str, last: str, run_id: str
) -> None:
    """Load every month in `first..last` that is absent; verify the ones present."""
    for table, (_, month_expr) in TABLES.items():
        cols = _stage(con, books, table, run_id)
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'raw' AND table_name = ?",
            [table],
        ).fetchone()[0]
        if not exists:
            con.execute(f"CREATE TABLE raw.{table} AS SELECT * FROM stage_{table} WHERE false")
        # A monthly table also has the group of rows with no month (a ticket not yet closed).
        groups = [None] if month_expr is None else [month_date(m) for m in months_between(first, last)] + [None]
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


def _reference_files(prices: Path, directory: Path) -> dict[str, Path]:
    """Every `*.csv` of the reference directory as `ref_<stem>`; the price list comes from `prices`."""
    files = {
        f"ref_{p.stem}": p for p in sorted(directory.glob("*.csv")) if p.name != PRICES_CSV
    }
    files["ref_model_prices"] = prices
    return files


def load_reference(
    con: duckdb.DuckDBPyConnection, prices: Path, reference_dir: Path | None = None
) -> None:
    """Load the reference files into `raw.ref_*`, each row with a `_row_hash` and no month.

    `reference_dir` is the directory they are read from: `MARGIN_REFERENCE_DIR`, else `data/reference`.

    A reference table is replaced by the file as it is now; a changed price enters the digests
    of the numbers that use it, which is how a restatement shows. An absent price
    list (the author has not sourced it yet) is an empty table, so the models still compile.
    """
    for table, path in _reference_files(prices, reference_dir or default_reference()).items():
        if path.is_file():
            cols = _columns(con, path)
            fields = " || chr(31) || ".join(f'coalesce("{c}", chr(0))' for c in cols)
            quoted = ", ".join(f'"{c}"' for c in cols)
            select = (
                f"SELECT {quoted}, "
                f"md5('{path.name}' || chr(31) || CAST(_ordinal AS VARCHAR) || chr(31) || {fields}) AS _row_hash, "
                "CAST(NULL AS DATE) AS _month, '' AS _load_id "
                f"FROM (SELECT *, row_number() OVER () AS _ordinal FROM {_read_sql(path.resolve())})"
            )
        elif table == "ref_model_prices":
            cols = PRICE_COLUMNS
            select = (
                "SELECT " + ", ".join(f'CAST(NULL AS VARCHAR) AS "{c}"' for c in cols)
                + ", CAST(NULL AS VARCHAR) AS _row_hash, CAST(NULL AS DATE) AS _month, "
                "CAST(NULL AS VARCHAR) AS _load_id WHERE false"
            )
        else:
            continue
        exists = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'raw' AND table_name = ?", [table],
        ).fetchone()[0]
        if exists:
            con.execute(f"DELETE FROM raw.{table}")
            con.execute(f"INSERT INTO raw.{table} {select}")
        else:
            con.execute(f"CREATE TABLE raw.{table} AS {select}")


CONFIG_FILES = {"allocation": ALLOCATION_FILE, "policy": POLICY_FILE}


def load_config(con: duckdb.DuckDBPyConnection, config_dir: Path) -> None:
    """Load the two signed configs into `raw.ref_config`, one row per file with a `_row_hash`.

    The table is replaced by the files as they are now; the gate has already checked that they
    are the ones signed. Rows are named by the config's short name (`allocation`, `policy`).
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS raw.ref_config (name VARCHAR, file VARCHAR, sha256 VARCHAR, "
        "content VARCHAR, _row_hash VARCHAR, _month DATE, _load_id VARCHAR)"
    )
    con.execute("DELETE FROM raw.ref_config")
    for ordinal, (name, file) in enumerate(CONFIG_FILES.items(), start=1):
        path = config_dir / file
        sha, text = sha256_file(path), path.read_text(encoding="utf-8")
        con.execute(
            "INSERT INTO raw.ref_config VALUES (?, ?, ?, ?, "
            "md5(? || chr(31) || ? || chr(31) || ? || chr(31) || ? || chr(31) || ?), NULL, '')",
            [name, file, sha, text, file, str(ordinal), name, sha, text],
        )
