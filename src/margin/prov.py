"""Provenance records (plan §6.4, §7.1): one per mart number, with a Merkle-shaped digest.

A leaf number hashes the raw source rows that can change its value; a composite
number hashes its direct children's (number_id, input_digest) pairs. The record
keeps the `source_query` that re-selects its inputs (a leaf) or the JSON list of
its child IDs (a composite), so `explain` can re-derive the digest.

`close` writes the families the month's `result_hash` covers (`cp/`, `pnl/`, `rollup/`, `flag/`
and the two that explain a flag, `cpr/` and `cohort/`) and, outside the hash, the month-less
`cfg/` and `ref/` records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path

import duckdb

from .errors import EXIT_PROVENANCE, MarginError
from .gen_config import Config
from .paths import ROOT
from .signoff import Signoff
from .warehouse import month_date

GL_LINE_TYPE = {"4000": "platform_fee", "4100": "usage", "4900": "credit"}
GL_CODES = tuple(GL_LINE_TYPE)  # the revenue codes
ALL_GL_CODES = (*GL_CODES, "5100", "5200", "5300")
# The families the month's result_hash covers; every other family is stored and never hashed.
RESULT_FAMILIES = ("cp", "pnl", "rollup", "flag", "cpr", "cohort")

LEAF_COMPONENTS = ("revenue", "inference_cost", "cloud_direct", "cloud_shared", "support_cost")
COMPONENTS = (*LEAF_COMPONENTS, "contribution_profit", "margin_pct", "trailing3_margin")
ROLLUP_SUMS = (*LEAF_COMPONENTS, "contribution_profit")
ROLLUP_COUNTS = ("accounts_firm", "accounts_soft", "accounts_active", "accounts_zero_revenue")
ROLLUP_STATS = ("revenue_p25", "revenue_p50", "margin_pct_p25", "margin_pct_p50", "margin_pct_p75")
CONFIG_NAMES = ("allocation", "policy")
# The column that names a row of a reference file in `ref/<file>/<key>`; the first column otherwise.
REF_KEYS = {"model_prices": "model", "support_rates": "tier"}

# The closed ID grammar (plan §7.1): the number registry is `prov.records` plus this set.
_MONTH = r"\d{4}-(?:0[1-9]|1[0-2])"
_ACCOUNT = r"[A-Za-z0-9_]+"
_ROLLUPS = "|".join((*ROLLUP_SUMS, *ROLLUP_COUNTS, *ROLLUP_STATS)) + "|accounts_status_change_[a-z_]+"
_CASE = "(?:full|half|flat)"
_QUARTER = "Q[1-4]"
SCENARIO_METRICS = ("inference_cost", "contribution_profit", "margin_pct", "above_floor")
BVB_ITEMS = ("build_cost", "build_cost_amortised", "run_cost_annual", "maintenance_allowance")
# family -> patterns; group 1 of a pattern, when the family has a month slot, is the month.
FAMILY_PATTERNS: dict[str, list[re.Pattern]] = {
    "cp": [re.compile(rf"^cp/({_MONTH})/{_ACCOUNT}/(?:{'|'.join(COMPONENTS)})$")],
    "pnl": [re.compile(rf"^pnl/({_MONTH})/(?:{'|'.join(ALL_GL_CODES)})$")],
    "rollup": [re.compile(rf"^rollup/({_MONTH})/(?:{_ROLLUPS})$")],
    "flag": [re.compile(rf"^flag/({_MONTH})/{_ACCOUNT}$")],
    "cpr": [re.compile(rf"^cpr/({_MONTH})/{_ACCOUNT}/[a-z_]+$")],
    "cohort": [re.compile(rf"^cohort/({_MONTH})/[a-z_]+_median$")],
    # The base month is the month slot of every scenario number.
    "scenario": [
        re.compile(rf"^scenario/({_MONTH})/{_CASE}/{_QUARTER}/{_ACCOUNT}/(?:{'|'.join(SCENARIO_METRICS)})$"),
        re.compile(rf"^scenario/({_MONTH})/{_CASE}/summary/{_ACCOUNT}/recovery_quarter$"),
        re.compile(rf"^scenario/({_MONTH})/summary/(?:monthly_rate|computed_change)$"),
        re.compile(rf"^scenario/({_MONTH})/{_CASE}/summary/(?:persistence_pct|accounts_recovered_{_QUARTER})$"),
    ],
    "ref": [re.compile(r"^ref/[a-z0-9_]+/[^/\s][^\s]*$")],
    "cfg": [re.compile(r"^cfg/[a-z_]+$")],
    "bvb": [re.compile(rf"^bvb/(?:{'|'.join(BVB_ITEMS)}|vendor_annual_cost/[^/\s]+)$")],
}
MONTH_LESS_FAMILIES = ("ref", "cfg", "bvb")


def family_of(number_id: str) -> str | None:
    """The family of an ID in the closed grammar, or None when the ID is outside it."""
    family = number_id.split("/", 1)[0]
    if any(p.match(number_id) for p in FAMILY_PATTERNS.get(family, ())):
        return family
    return None


def is_registered(number_id: str) -> bool:
    return family_of(number_id) is not None


def month_of_id(number_id: str) -> str | None:
    """The month slot of an ID (a scenario's base month), or None for a month-less ID or one
    outside the grammar."""
    family = family_of(number_id)
    if family is None or family in MONTH_LESS_FAMILIES:
        return None
    for pattern in FAMILY_PATTERNS[family]:
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


CTL_PROV = "CTL-PROV"
RECORD_COLS = (
    "number_id", "month", "value_cents", "value_text", "unit", "step", "git_sha", "producer_kind",
    "producer_id", "input_digest", "input_row_count", "source_query", "checks_passed", "signer",
    "signoff_ref", "run_id",
)


def run_source_query(con: duckdb.DuckDBPyConnection, query: str) -> list[tuple]:
    """Run a stored `source_query`: one SELECT statement or nothing. The text lives in a table a
    person can edit, so a record whose query is anything else is a provenance failure."""
    try:
        statements = con.extract_statements(query)
    except duckdb.Error as exc:
        raise MarginError(EXIT_PROVENANCE, f"source_query does not parse: {exc}") from None
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise MarginError(EXIT_PROVENANCE, "source_query is not a single SELECT statement")
    return con.execute(query).fetchall()


def fetch_record(con: duckdb.DuckDBPyConnection, number_id: str) -> dict | None:
    row = con.execute(
        f"SELECT {', '.join(RECORD_COLS)} FROM prov.records WHERE number_id = ?", [number_id]
    ).fetchone()
    return None if row is None else dict(zip(RECORD_COLS, row))


def rederive(con: duckdb.DuckDBPyConnection, rec: dict, get_child) -> tuple[str, list[tuple] | list[dict], int]:
    """Re-derive a stored record's digest: a leaf from the rows its `source_query` selects, a
    composite from its children's stored digests. `get_child(id)` returns a record or None.

    Returns (digest, rows or child records, row/child count). Raises `MarginError` (exit 6) for a
    query that is not a SELECT, or a child with no record.
    """
    query = rec["source_query"] or ""
    if is_composite(query):
        try:
            child_ids = json.loads(query)
        except ValueError:
            raise MarginError(EXIT_PROVENANCE, f"{rec['number_id']}: child list does not parse") from None
        children = []
        for child_id in child_ids:
            child = get_child(child_id)
            if child is None:
                raise MarginError(EXIT_PROVENANCE, f"provenance incomplete: no record for {child_id}")
            children.append(child)
        return digest_children([(c["number_id"], c["input_digest"]) for c in children]), children, len(children)
    rows = run_source_query(con, query)
    return digest_rows([r[1] for r in rows]), rows, len(rows)


# ------------------------------------------------------------------ source queries
#
# A leaf's source_query returns (source_table, _row_hash, row as JSON) for every raw row that can
# change the number, ordered. It is stored on the record and re-run by `explain`.


def _q(value: str) -> str:
    return value.replace("'", "''")


def _d(month: str) -> str:
    return f"DATE '{month_date(month)}'"


def _frag(table: str, where: str) -> str:
    return f"SELECT '{table}' AS source_table, r._row_hash, to_json(r) AS row FROM {table} r WHERE {where}"


def _union(frags: list[str]) -> str:
    return " UNION ".join(frags) + " ORDER BY 1, 2"


def _tenant(alias: str = "r") -> str:
    return f"nullif(json_extract_string(nullif({alias}.\"Tags\", ''), '$.tenant'), '')"


def _invoices(month: str, account: str | None = None) -> str:
    cond = f"r._month = {_d(month)}" + (f" AND r.account_id = '{_q(account)}'" if account else "")
    return _frag("raw.invoices", cond)


def _usage(month: str) -> str:
    return _frag("raw.usage_monthly", f"r._month = {_d(month)}")


def _inference(account: str, month: str) -> list[str]:
    """The account's usage pools: every usage and provider-invoice row of the models it used, and
    those models' price rows, since the pool's other accounts and its invoice set its share."""
    models = f"(SELECT model FROM raw.usage_monthly WHERE account_id = '{_q(account)}' AND _month = {_d(month)})"
    return [
        _frag("raw.usage_monthly", f"r._month = {_d(month)} AND r.model IN {models}"),
        _frag("raw.provider_invoices", f"r._month = {_d(month)} AND r.model IN {models}"),
        _frag("raw.ref_model_prices", f"r.model IN {models}"),
    ]


def _direct(account: str, month: str) -> list[str]:
    return [_frag("raw.cloud_focus", f"r._month = {_d(month)} AND {_tenant()} = '{_q(account)}'")]


def _shared(month: str, rule: str) -> list[str]:
    """The whole untagged FOCUS pool and the weight rows of `rule`."""
    pool = _frag("raw.cloud_focus", f"r._month = {_d(month)} AND {_tenant()} IS NULL")
    if rule == "by_revenue":
        return [pool, _invoices(month)]
    if rule in ("by_tokens", "equal_usage"):
        return [pool, _usage(month)]
    raise ValueError(f"unknown allocation rule {rule}")


def _support(account: str, month: str) -> list[str]:
    tickets = f"r._month = {_d(month)} AND r.account_id = '{_q(account)}'"
    tiers = f"(SELECT tier FROM raw.support_tickets WHERE account_id = '{_q(account)}' AND _month = {_d(month)})"
    return [_frag("raw.support_tickets", tickets), _frag("raw.ref_support_rates", f"r.tier IN {tiers}")]


def _account_row(account: str) -> str:
    return _frag("raw.accounts", f"r.account_id = '{_q(account)}'")


def revenue_query(account_id: str, month: str) -> str:
    return _union([_invoices(month, account_id)])


def component_query(component: str, account: str, month: str, signed_rule: str) -> str:
    frags = {
        "revenue": lambda: [_invoices(month, account)],
        "inference_cost": lambda: _inference(account, month),
        "cloud_direct": lambda: _direct(account, month),
        "cloud_shared": lambda: _shared(month, signed_rule),
        "support_cost": lambda: _support(account, month),
    }[component]()
    return _union(frags)


def cpr_query(account: str, month: str, rule: str) -> str:
    """Everything the account's contribution profit under `rule` and its flag depend on."""
    return _union([
        _invoices(month, account), *_inference(account, month), *_direct(account, month),
        *_shared(month, rule), *_support(account, month), _account_row(account),
    ])


def cohort_query(month: str, code: str) -> str:
    """The rows behind a cohort median: who is in the cohort (invoices and usage), the discounts
    that restore list-price revenue, and the cost rows of the driver."""
    frags = [
        _invoices(month), _usage(month),
        _frag("raw.accounts", f"r.account_id IN (SELECT account_id FROM raw.invoices WHERE _month = {_d(month)})"),
    ]
    frags += {
        "DEEP_DISCOUNT": [],
        "MODEL_MIX": [_frag("raw.provider_invoices", f"r._month = {_d(month)}"), _frag("raw.ref_model_prices", "TRUE")],
        "SUPPORT_LOAD": [_frag("raw.support_tickets", f"r._month = {_d(month)}"), _frag("raw.ref_support_rates", "TRUE")],
        "DEDICATED_CLOUD": [_frag("raw.cloud_focus", f"r._month = {_d(month)} AND {_tenant()} IS NOT NULL")],
    }[code]
    return _union(frags)


def pnl_query(gl_code: str, month: str) -> str:
    """The ledger rows of the code and the account-side rows its accounts total is built from."""
    frags = [_frag("raw.gl", f"r.gl_code = '{gl_code}' AND r._month = {_d(month)}")]
    if gl_code in GL_LINE_TYPE:
        frags.append(_frag("raw.invoices", f"r.line_type = '{GL_LINE_TYPE[gl_code]}' AND r._month = {_d(month)}"))
    elif gl_code == "5100":
        frags += [_frag("raw.provider_invoices", f"r._month = {_d(month)}"), _usage(month),
                  _frag("raw.ref_model_prices", "TRUE")]
    elif gl_code == "5200":
        frags.append(_frag("raw.cloud_focus", f"r._month = {_d(month)}"))
    else:
        frags += [_frag("raw.support_tickets", f"r._month = {_d(month)}"), _frag("raw.ref_support_rates", "TRUE")]
    return _union(frags)


def cfg_query(name: str) -> str:
    return _union([_frag("raw.ref_config", f"r.name = '{_q(name)}'")])


# ------------------------------------------------------------------ building the records


def _base(month: str | None, *, checks: list[str], run_id: str, signoff: Signoff) -> dict:
    return {
        "month": month_date(month) if month else None, "git_sha": git_sha(),
        "producer_kind": os.environ.get("MARGIN_PRODUCER_KIND", "workflow"),
        "producer_id": "margin close", "checks_passed": checks, "signer": signoff.signer,
        "signoff_ref": signoff.ref, "run_id": run_id,
    }


class _Builder:
    """Collects the records of one month; a composite finds its children here or in `known`."""

    def __init__(self, con: duckdb.DuckDBPyConnection, base: dict, known: dict[str, dict]):
        self.con, self.base, self.known = con, base, known
        self.records: dict[str, dict] = {}

    def get(self, number_id: str) -> dict:
        return self.records.get(number_id) or self.known[number_id]

    def _add(self, number_id, value_cents, value_text, unit, step, digest, count, source_query) -> None:
        text = value_text if value_text is not None else (None if value_cents is None else str(value_cents))
        self.records[number_id] = {
            **self.base, "number_id": number_id, "value_cents": value_cents, "value_text": text,
            "unit": unit, "step": step, "input_digest": digest, "input_row_count": count,
            "source_query": source_query,
        }

    def leaf(self, number_id, query, *, value_cents=None, value_text=None, unit="cents", step) -> None:
        hashes = [r[1] for r in self.con.execute(query).fetchall()]
        self._add(number_id, value_cents, value_text, unit, step, digest_rows(hashes), len(hashes), query)

    def composite(self, number_id, child_ids, *, value_cents=None, value_text=None, unit="cents", step) -> None:
        children = [self.get(c) for c in child_ids]
        digest = digest_children([(c["number_id"], c["input_digest"]) for c in children])
        self._add(number_id, value_cents, value_text, unit, step, digest, len(children),
                  json.dumps(sorted(child_ids)))


def _pct_text(value: Decimal | None) -> str:
    return "n/a" if value is None else str(value)


def _nearest_rank(values: list, p: int):
    """The value at 1-based rank ceil(p * n / 100) of the sorted values; None for none."""
    if not values:
        return None
    return sorted(values)[-(-p * len(values) // 100) - 1]


def config_records(con: duckdb.DuckDBPyConnection, base: dict) -> list[dict]:
    """`cfg/<name>`: a leaf over the config file's one row; the value is the file's SHA-256."""
    out = []
    for name in CONFIG_NAMES:
        query = cfg_query(name)
        rows = con.execute(query).fetchall()
        sha = con.execute("SELECT sha256 FROM raw.ref_config WHERE name = ?", [name]).fetchone()[0]
        out.append({
            **base, "number_id": f"cfg/{name}", "value_cents": None, "value_text": sha, "unit": "sha256",
            "step": "margin.load", "input_digest": digest_rows([r[1] for r in rows]),
            "input_row_count": len(rows), "source_query": query,
        })
    return out


def reference_records(con: duckdb.DuckDBPyConnection, *, run_id: str, signoff: Signoff) -> list[dict]:
    """The month-less records: `cfg/<name>` and `ref/<file>/<key>`, one leaf per row key."""
    base = _base(None, checks=[], run_id=run_id, signoff=signoff)
    out = config_records(con, base)
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw' "
        "AND table_name LIKE 'ref\\_%' ESCAPE '\\' AND table_name <> 'ref_config' ORDER BY table_name"
    ).fetchall()]
    for table in tables:
        stem = table[len("ref_"):]
        columns = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'raw' "
            "AND table_name = ? AND column_name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY ordinal_position", [table]
        ).fetchall()]
        key = REF_KEYS.get(stem, columns[0])
        for (value,) in con.execute(
            f'SELECT DISTINCT "{key}" FROM raw.{table} WHERE "{key}" IS NOT NULL ORDER BY 1'
        ).fetchall():
            query = _union([_frag(f"raw.{table}", f'r."{key}" = \'{_q(value)}\'')])
            rows = con.execute(query).fetchall()
            shown = [{k: v for k, v in json.loads(r[2]).items() if not k.startswith("_")} for r in rows]
            out.append({
                **base, "number_id": f"ref/{stem}/{value}", "value_cents": None,
                "value_text": json.dumps(shown, sort_keys=True), "unit": "ref", "step": "margin.load",
                "input_digest": digest_rows([r[1] for r in rows]), "input_row_count": len(rows),
                "source_query": query,
            })
    return sorted(out, key=lambda r: r["number_id"])


def build_records(
    con: duckdb.DuckDBPyConnection, month: str, *, checks: list[str], run_id: str,
    signoff: Signoff, config: Config, known: dict[str, dict] | None = None,
) -> list[dict]:
    """Derive this month's provenance records from the marts. Writes nothing.

    `known` holds the records of the months already built in this run, by number ID: the trailing
    margin of a month reads the contribution profit and revenue records of the months before it.
    """
    d = month_date(month)
    base = _base(month, checks=checks, run_id=run_id, signoff=signoff)
    rules, signed = list(config.allocation.rules), config.allocation.recommended
    codes = [drv.code for drv in config.policy.drivers]
    cfg_ids = [f"cfg/{name}" for name in CONFIG_NAMES]
    b = _Builder(con, base, {**(known or {}), **{r["number_id"]: r for r in config_records(con, base)}})

    accounts = con.execute(
        "SELECT account_id, revenue_cents, inference_cost_cents, cloud_direct_cents, cloud_shared_cents, "
        "support_cost_cents, contribution_profit_cents, margin_pct, trailing3_margin_pct "
        "FROM mart.fct_contribution_profit WHERE month = ? ORDER BY account_id", [d],
    ).fetchall()
    ids = [a[0] for a in accounts]
    window: dict[str, list[str]] = {}
    for account_id, m in con.execute(
        "SELECT account_id, strftime(month, '%Y-%m') FROM mart.fct_contribution_profit "
        "WHERE month BETWEEN CAST(? AS DATE) - INTERVAL 2 MONTH AND CAST(? AS DATE) ORDER BY 2", [d, d]
    ).fetchall():
        window.setdefault(account_id, []).append(m)

    def cp(account: str, component: str) -> str:
        return f"cp/{month}/{account}/{component}"

    for account, *values in accounts:
        for component, value in zip(LEAF_COMPONENTS, values):
            b.leaf(cp(account, component), component_query(component, account, month, signed),
                   value_cents=value, step="dbt:fct_contribution_profit")
        profit, margin, trailing = values[5], values[6], values[7]
        b.composite(cp(account, "contribution_profit"), [cp(account, c) for c in LEAF_COMPONENTS],
                    value_cents=profit, step="dbt:fct_contribution_profit")
        b.composite(cp(account, "margin_pct"), [cp(account, "contribution_profit"), cp(account, "revenue")],
                    value_text=_pct_text(margin), unit="percent", step="dbt:fct_contribution_profit")
        b.composite(
            cp(account, "trailing3_margin"),
            [f"cp/{m}/{account}/{c}" for m in window[account] for c in ("contribution_profit", "revenue")],
            value_text=_pct_text(trailing), unit="percent", step="dbt:fct_contribution_profit",
        )

    for gl_code, diff in con.execute(
        f"SELECT gl_code, diff_cents FROM mart.fct_pnl_tieout WHERE month = ? "
        f"AND gl_code IN ({', '.join(repr(c) for c in ALL_GL_CODES)}) ORDER BY gl_code", [d],
    ).fetchall():
        b.leaf(f"pnl/{month}/{gl_code}", pnl_query(gl_code, month), value_cents=diff, step="dbt:fct_pnl_tieout")

    below = {}
    for rule, account, profit, is_below in con.execute(
        "SELECT rule, account_id, contribution_profit_cents, below_floor FROM mart.fct_contribution_by_rule "
        "WHERE month = ? ORDER BY rule, account_id", [d],
    ).fetchall():
        below[(rule, account)] = is_below
        b.leaf(f"cpr/{month}/{account}/{rule}", cpr_query(account, month, rule), value_cents=profit,
               step="dbt:fct_contribution_by_rule")

    medians = dict(con.execute(
        "SELECT code, median FROM intermediate.int_cohort_medians WHERE month = ?", [d]).fetchall())
    for code in codes:
        median = medians.get(code)
        b.leaf(f"cohort/{month}/{code.lower()}_median", cohort_query(month, code),
               value_text="n/a" if median is None else f"{Decimal(median).scaleb(-9):.9f}",
               unit="ratio", step="dbt:int_cohort_medians")

    flags = {a: (f, r) for a, f, r in con.execute(
        "SELECT account_id, flag, reason_code FROM mart.fct_flags WHERE month = ?", [d]).fetchall()}
    for account in ids:
        flag, reason = flags[account]
        b.composite(
            f"flag/{month}/{account}",
            [f"cpr/{month}/{account}/{r}" for r in rules]
            + [f"cohort/{month}/{c.lower()}_median" for c in codes] + cfg_ids,
            value_text=flag if reason is None else f"{flag}:{reason}", unit="flag", step="dbt:fct_flags",
        )

    # Month totals and counts: composites over the numbers they summarise. `by_id` rows are
    # (account_id, revenue, inference, direct, shared, support, profit, margin_pct, trailing3).
    by_id = {a[0]: a for a in accounts}
    for index, metric in enumerate(ROLLUP_SUMS, start=1):
        b.composite(f"rollup/{month}/{metric}", [cp(a, metric) for a in ids],
                    value_cents=sum(by_id[a][index] for a in ids), step="margin.prov.rollup")
    for flag in ("firm", "soft"):
        b.composite(f"rollup/{month}/accounts_{flag}", [f"flag/{month}/{a}" for a in ids],
                    value_cents=sum(1 for a in ids if flags[a][0] == flag), unit="accounts",
                    step="margin.prov.rollup")
    revenue_children = [cp(a, "revenue") for a in ids]
    b.composite(f"rollup/{month}/accounts_active", revenue_children, value_cents=len(ids), unit="accounts",
                step="margin.prov.rollup")
    b.composite(f"rollup/{month}/accounts_zero_revenue", revenue_children,
                value_cents=sum(1 for a in ids if by_id[a][1] == 0), unit="accounts",
                step="margin.prov.rollup")
    for rule in rules:
        children = sorted({f"cpr/{month}/{a}/{r}" for a in ids for r in (rule, signed)})
        b.composite(f"rollup/{month}/accounts_status_change_{rule}", children,
                    value_cents=sum(1 for a in ids if below[(rule, a)] != below[(signed, a)]),
                    unit="accounts", step="margin.prov.rollup")

    cohort = [r[0] for r in con.execute(
        "SELECT p.account_id FROM mart.fct_contribution_profit p "
        "JOIN intermediate.int_account_facts f ON f.month = p.month AND f.account_id = p.account_id "
        "WHERE p.month = ? AND f.has_usage AND p.revenue_cents > 0 ORDER BY 1", [d]).fetchall()]
    for p in (25, 50):
        value = _nearest_rank([by_id[a][1] for a in cohort], p)
        b.composite(f"rollup/{month}/revenue_p{p}", [cp(a, "revenue") for a in cohort], value_cents=value,
                    value_text="n/a" if value is None else None, step="margin.prov.rollup")
    for p in (25, 50, 75):
        value = _nearest_rank([by_id[a][7] for a in cohort], p)
        b.composite(f"rollup/{month}/margin_pct_p{p}", [cp(a, "margin_pct") for a in cohort],
                    value_text=_pct_text(value), unit="percent", step="margin.prov.rollup")
    return sorted(b.records.values(), key=lambda r: r["number_id"])


def result_hash(records: list[dict]) -> str:
    """SHA-256 over number_id, value, unit and input_digest, ordered by number_id.

    git_sha, producer, signer and run_id are stored on the record and never hashed, so a new commit
    does not change a result. Records outside RESULT_FAMILIES (`cfg/`, `ref/`) are ignored.
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
    """Store records; an ID outside the closed grammar is refused (the registry is closed)."""
    unregistered = [r["number_id"] for r in records if not is_registered(r["number_id"])]
    if unregistered:
        raise MarginError(EXIT_PROVENANCE, f"unregistered number ID {unregistered[0]}")
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


def write_config_records(con: duckdb.DuckDBPyConnection, *, run_id: str, signoff: Signoff) -> None:
    """Store the `cfg/` records: the children of every `flag/` record, so they must exist when the
    month's provenance is checked. Idempotent; they sit outside `result_hash`."""
    write_records(con, config_records(con, _base(None, checks=[], run_id=run_id, signoff=signoff)))


# ------------------------------------------------------------------ CTL-PROV


def expected_ids(con: duckdb.DuckDBPyConnection, month: str, config: Config) -> list[str]:
    """Every mart number of the month: each must have exactly one record."""
    d = month_date(month)
    rules = list(config.allocation.rules)
    accounts = [a for (a,) in con.execute(
        "SELECT account_id FROM mart.fct_contribution_profit WHERE month = ? ORDER BY 1", [d]).fetchall()]
    expected = [f"cp/{month}/{a}/{c}" for a in accounts for c in COMPONENTS]
    expected += [f"flag/{month}/{a}" for a in accounts]
    expected += [f"cpr/{month}/{a}/{r}" for a in accounts for r in rules]
    expected += [f"pnl/{month}/{code}" for code in ALL_GL_CODES]
    expected += [
        f"rollup/{month}/{m}"
        for m in (*ROLLUP_SUMS, *ROLLUP_COUNTS, *ROLLUP_STATS, *(f"accounts_status_change_{r}" for r in rules))
    ]
    expected += [f"cohort/{month}/{drv.code.lower()}_median" for drv in config.policy.drivers]
    return expected


def control(
    con: duckdb.DuckDBPyConnection, month: str, config: Config, *, expected_hash: str | None = None,
) -> dict:
    """CTL-PROV over the *stored* records: provenance complete and every digest re-derivable.

    Violations, by number ID: an ID outside the registry; a mart number with no complete record;
    a record whose digest, or row/child count, does not re-derive; and, given `expected_hash`,
    a month whose stored records no longer hash to it. Returns the control-result shape
    (id, status, violating_rows, detail); it raises nothing for a violation.
    """
    d = month_date(month)
    problems: dict[str, str] = {}
    stored = {
        r["number_id"]: r for r in (
            dict(zip(RECORD_COLS, row)) for row in con.execute(
                f"SELECT {', '.join(RECORD_COLS)} FROM prov.records WHERE month = ? OR month IS NULL ORDER BY 1", [d]
            ).fetchall()
        )
    }
    month_records = [r for r in stored.values() if r["month"] is not None]
    for (number_id,) in con.execute("SELECT number_id FROM prov.records").fetchall():
        if not is_registered(number_id):
            problems[number_id] = "not in the number registry"
    for r in month_records:
        if month_of_id(r["number_id"]) != month:
            problems.setdefault(r["number_id"], "month column does not match the number ID")
    for number_id in expected_ids(con, month, config):
        r = stored.get(number_id)
        if r is None or not r["input_digest"] or not r["source_query"] or (
            r["value_cents"] is None and r["value_text"] is None
        ):
            problems.setdefault(number_id, "no complete provenance record")

    def get_child(child_id: str) -> dict | None:
        return stored.get(child_id) or fetch_record(con, child_id)

    # The month's records, and the month-less records they hang on (the config leaves).
    pending = {r["number_id"] for r in month_records}
    for r in month_records:
        if is_composite(r["source_query"] or ""):
            try:
                pending.update(c for c in json.loads(r["source_query"]) if c in stored)
            except ValueError:
                pass
    for number_id in sorted(pending):
        r = stored[number_id]
        if number_id in problems or not r["source_query"]:
            continue
        try:
            digest, _, count = rederive(con, r, get_child)
        except MarginError as err:
            problems[number_id] = err.message
            continue
        if digest != r["input_digest"]:
            problems[number_id] = f"digest does not re-derive: stored {r['input_digest']}, derived {digest}"
        elif count != r["input_row_count"]:
            problems[number_id] = f"input_row_count {r['input_row_count']} but {count} inputs"
    if expected_hash is not None:
        got = result_hash(month_records)
        if got != expected_hash:
            problems[f"result_hash/{month}"] = f"stored records hash to {got}, expected {expected_hash}"
    detail = [f"{number_id}: {why}" for number_id, why in sorted(problems.items())]
    return {"id": CTL_PROV, "status": "fail" if detail else "pass", "violating_rows": len(detail), "detail": detail}
