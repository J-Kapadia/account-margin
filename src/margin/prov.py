"""Provenance records (plan §6.4, §7.1): one per mart number, with a Merkle-shaped digest.

A leaf number hashes the raw source rows that can change its value; a composite
number hashes its direct children's (number_id, input_digest) pairs. The record
keeps the `source_query` that re-selects its inputs, so `explain` can re-derive
the digest. Phase 0 writes the `cp/`, `pnl/` and `rollup/` families it needs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import duckdb

from .paths import ROOT
from .warehouse import month_date

GL_LINE_TYPE = {"4000": "platform_fee", "4100": "usage", "4900": "credit"}
GL_CODES = tuple(GL_LINE_TYPE)
RESULT_FAMILIES = ("cp", "pnl", "rollup", "flag")

# The closed ID grammar of plan §7.1, restricted to what Phase 0 can write.
_MONTH = r"\d{4}-(?:0[1-9]|1[0-2])"
ID_PATTERNS = [
    re.compile(rf"^cp/({_MONTH})/[A-Za-z0-9_]+/revenue$"),
    re.compile(rf"^pnl/({_MONTH})/(?:{'|'.join(GL_CODES)})$"),
    re.compile(rf"^rollup/({_MONTH})/revenue$"),
]


def month_of_id(number_id: str) -> str | None:
    """The month a number ID belongs to, or None if the ID is outside the grammar."""
    for pattern in ID_PATTERNS:
        match = pattern.match(number_id)
        if match:
            return match.group(1)
    return None


def git_sha() -> str:
    """MARGIN_GIT_SHA, else `git rev-parse HEAD` in this checkout, else the .gitsha file."""
    if os.environ.get("MARGIN_GIT_SHA"):
        return os.environ["MARGIN_GIT_SHA"]
    try:
        top = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if Path(top).resolve() == ROOT:
            return subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    marker = ROOT / ".gitsha"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text and not text.startswith("$Format"):
            return text
    return "unknown"


def digest_rows(row_hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(row_hashes)).encode("utf-8")).hexdigest()


def digest_children(pairs: list[tuple[str, str]]) -> str:
    return hashlib.sha256("\n".join(f"{i}\t{d}" for i, d in sorted(pairs)).encode()).hexdigest()


def is_composite(source_query: str) -> bool:
    """A composite's source_query is the JSON list of its child IDs; a leaf's is SQL."""
    return source_query.startswith("[")


def _q(value: str) -> str:
    return value.replace("'", "''")


def _source_rows(con: duckdb.DuckDBPyConnection, source_query: str) -> list[tuple[str, str, str]]:
    return con.execute(source_query).fetchall()


def revenue_query(account_id: str, month: str) -> str:
    return (
        "SELECT 'raw.invoices' AS source_table, _row_hash, to_json(i) AS row "
        f"FROM raw.invoices i WHERE account_id = '{_q(account_id)}' "
        f"AND _month = DATE '{month_date(month)}' ORDER BY _row_hash"
    )


def pnl_query(gl_code: str, month: str) -> str:
    line_type = GL_LINE_TYPE[gl_code]
    return (
        "SELECT 'raw.gl' AS source_table, _row_hash, to_json(g) AS row FROM raw.gl g "
        f"WHERE gl_code = '{gl_code}' AND _month = DATE '{month_date(month)}' "
        "UNION ALL "
        "SELECT 'raw.invoices', _row_hash, to_json(i) FROM raw.invoices i "
        f"WHERE line_type = '{line_type}' AND _month = DATE '{month_date(month)}' "
        "ORDER BY 1, 2"
    )


def build_records(
    con: duckdb.DuckDBPyConnection, month: str, *, checks: list[str], run_id: str
) -> list[dict]:
    """Derive this month's provenance records from the marts. Writes nothing."""
    sha = git_sha()
    producer_kind = os.environ.get("MARGIN_PRODUCER_KIND", "workflow")
    base = {
        "month": month_date(month), "unit": "cents", "git_sha": sha,
        "producer_kind": producer_kind, "producer_id": "margin close",
        "checks_passed": checks, "signer": None, "signoff_ref": None, "run_id": run_id,
    }
    records = []

    def leaf(number_id, value, step, query):
        hashes = [r[1] for r in _source_rows(con, query)]
        records.append({
            **base, "number_id": number_id, "value_cents": value, "value_text": str(value),
            "step": step, "input_digest": digest_rows(hashes), "input_row_count": len(hashes),
            "source_query": query,
        })

    accounts = con.execute(
        "SELECT account_id, revenue_cents FROM mart.fct_contribution_profit "
        "WHERE month = ? ORDER BY account_id", [month_date(month)],
    ).fetchall()
    for account_id, revenue in accounts:
        leaf(f"cp/{month}/{account_id}/revenue", revenue, "dbt:fct_contribution_profit",
             revenue_query(account_id, month))

    tieout = con.execute(
        "SELECT gl_code, diff_cents FROM mart.fct_pnl_tieout "
        f"WHERE month = ? AND gl_code IN ({', '.join(repr(c) for c in GL_CODES)}) ORDER BY gl_code",
        [month_date(month)],
    ).fetchall()
    for gl_code, diff in tieout:
        leaf(f"pnl/{month}/{gl_code}", diff, "dbt:fct_pnl_tieout", pnl_query(gl_code, month))

    children = [r for r in records if r["number_id"].startswith("cp/")]
    records.append({
        **base, "number_id": f"rollup/{month}/revenue",
        "value_cents": sum(r["value_cents"] for r in children),
        "value_text": str(sum(r["value_cents"] for r in children)),
        "step": "margin.prov.rollup",
        "input_digest": digest_children([(r["number_id"], r["input_digest"]) for r in children]),
        "input_row_count": len(children),
        "source_query": json.dumps(sorted(r["number_id"] for r in children)),
    })
    return sorted(records, key=lambda r: r["number_id"])


def result_hash(records: list[dict]) -> str:
    """SHA-256 over number_id, value, unit and input_digest, ordered by number_id.

    git_sha, producer and run_id are stored on the record and never hashed, so a
    new commit does not change a result (plan §7.1).
    """
    lines = [
        f"{r['number_id']}|{r['value_cents'] if r['value_cents'] is not None else r['value_text']}"
        f"|{r['unit']}|{r['input_digest']}"
        for r in sorted(records, key=lambda r: r["number_id"])
        if r["number_id"].split("/", 1)[0] in RESULT_FAMILIES
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def month_input_digest(records: list[dict]) -> str:
    """The month's Merkle input digest over its leaf records."""
    leaves = [r for r in records if not is_composite(r["source_query"])]
    return digest_children([(r["number_id"], r["input_digest"]) for r in leaves])


def write_records(con: duckdb.DuckDBPyConnection, records: list[dict]) -> None:
    for r in records:
        con.execute(
            "INSERT OR REPLACE INTO prov.records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                r["number_id"], r["month"], r["value_cents"], r["value_text"], r["unit"],
                r["step"], r["git_sha"], r["producer_kind"], r["producer_id"],
                r["input_digest"], r["input_row_count"], r["source_query"],
                r["checks_passed"], r["signer"], r["signoff_ref"], r["run_id"],
            ],
        )
