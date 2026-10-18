"""`memo`: a Jinja2 page whose every number goes through `num(id)`.

The memo is a reader: it opens the warehouse read-only and writes nothing to it.
`num` renders an ID found in `prov.records` as a span with `data-num-id` and the
unrounded `data-value`, and fails the build (`CHK-MEMO-NUM`, exit 3) for an ID with no record.
`pct` is the same span with a percent sign after a figure. The queries here choose which
accounts and which rules a section shows; the template names the IDs and `num` supplies every value.

`build` also writes the site around the page: one explain page for every number ID the page renders
and for every ID beneath them, `sql/fct_contribution_profit.sql` (the compiled model `close` stored),
the month's `values.csv`, and `llms.txt` and `index.html` listing the months whose memo exists.
"""

from __future__ import annotations

import csv
import io
import json
import re
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import duckdb
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape

from . import prov, scenario
from . import explain as explain_mod
from .dbt_runner import PUBLISHED_MODEL
from .errors import EXIT_CONTROL, EXIT_PRECONDITION, MarginError
from .money import format_cents

TEMPLATES = Path(__file__).resolve().parent / "templates"
REPOSITORY = "https://github.com/J-Kapadia/account-margin"
# How many accounts the top and bottom tables show at most.
RANKED = 5
# How many source rows an explain page prints; the rest are counted and left to `margin explain`.
EXPLAIN_ROWS = 25
OWNER = "finance owner (synthetic)"
DEFINITION = "contribution profit in cents is `contribution_cents()` in dbt/macros/contribution_cents.sql"
NUM_ID = re.compile(r'data-num-id="([^"]+)"')
CITE_ID = re.compile(r'data-ref-id="([^"]+)"')
MONTH_DIR = re.compile(r"\d{4}-\d{2}")
# The action for each firm-flagged account, by reason code: (machine key, text shown).
REPRICE_AT_RENEWAL = ("reprice_at_renewal", "Reprice at renewal")
MIGRATE = ("migrate_per_model_plan", "Migrate to a plan priced per model")
LET_CHURN = ("let_churn", "Let churn")
REPRICE = ("reprice", "Reprice")
REVIEW = ("review", "Review the account")
ACTIONS = {"DEEP_DISCOUNT": REPRICE_AT_RENEWAL, "DEDICATED_CLOUD": REPRICE_AT_RENEWAL, "MODEL_MIX": MIGRATE}


def _registry(con: duckdb.DuckDBPyConnection, month: str) -> dict[str, dict]:
    """The month's records (the `scenario/` ones carry the base month) and the month-less `ref/` and
    `bvb/` records the scenario's trend and the build-versus-buy section are cited from."""
    rows = con.execute(
        "SELECT number_id, value_cents, value_text, unit FROM prov.records "
        "WHERE month = ? OR (month IS NULL AND (starts_with(number_id, 'bvb/') OR starts_with(number_id, 'ref/')))",
        [f"{month}-01"],
    ).fetchall()
    return {r[0]: {"value_cents": r[1], "value_text": r[2], "unit": r[3]} for r in rows}


def _record(registry: dict[str, dict], number_id: str) -> dict:
    rec = registry.get(number_id)
    if rec is None:
        raise MarginError(
            EXIT_CONTROL, f"memo uses {number_id}, which has no provenance record",
            failed_controls=[{"id": "CHK-MEMO-NUM", "violating_rows": 1, "detail": [number_id]}],
        )
    return rec


def _percent(ratio: str) -> str:
    """A ratio as a percentage to two places, half-even: `-0.0231` is `-2.31%`."""
    return f"{(Decimal(ratio) * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)}%"


def make_env(registry: dict[str, dict]) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
    )

    def span(number_id: str, suffix: str = "") -> Markup:
        rec = _record(registry, number_id)
        cents = rec["value_cents"]
        value = cents if cents is not None else rec["value_text"]
        shown = format_cents(cents) if cents is not None and rec["unit"] == "cents" else rec["value_text"]
        if rec["unit"] == "ratio":
            shown = _percent(shown)
        if suffix and shown != "n/a":
            shown = f"{shown}{suffix}"
        return Markup('<a href="../explain/{}.html"><span data-num-id="{}" data-value="{}">{}</span></a>').format(
            number_id, number_id, str(value), shown)

    def cite(text: str, number_id: str) -> Markup:
        """A literal quoted from a reference row: not a computed number, but tied to the record it came from."""
        _record(registry, number_id)
        return Markup('<a href="../explain/{}.html"><span data-literal="cite" data-ref-id="{}">{}</span></a>').format(
            number_id, number_id, text)

    env.filters["num"] = span
    env.filters["cite"] = lambda text, number_id: cite(text, number_id)
    env.filters["pct"] = lambda number_id: span(number_id, "%")
    return env


def empty_sections() -> dict:
    """The per-section data of a month with nothing in it; `render` fills what a caller leaves out."""
    return {
        "top": [], "bottom": [], "firm": [], "soft": [], "zero_revenue": [], "actions": [],
        "rules": [], "recommended": "", "spread": [], "changes": [], "controls": [], "scenario": None,
        "bvb_present": False, "bvb_measures": [], "bvb_vendors": [],
    }


def render(month: str, accounts: list[dict], registry: dict[str, dict],
           sections: dict | None = None) -> str:
    template = make_env(registry).get_template("memo.html.j2")
    return template.render(month=month, accounts=accounts, gl_codes=prov.GL_CODES,
                           repository=REPOSITORY, **{**empty_sections(), **(sections or {})})


def action_for(reason_code: str | None, revenue_cents: int, revenue_p25: int | None) -> tuple[str, str]:
    """The action for a firm-flagged account. Support load below the cohort's lower quartile of
    revenue is not worth repricing, so the account is let go; any other support load is repriced."""
    if reason_code == "SUPPORT_LOAD":
        return LET_CHURN if revenue_p25 is not None and revenue_cents < revenue_p25 else REPRICE
    return ACTIONS.get(reason_code or "", REVIEW)


def _accounts(con: duckdb.DuckDBPyConnection, month: str) -> list[dict]:
    rows = con.execute(
        "SELECT p.account_id, a.display_name, p.revenue_cents, p.contribution_profit_cents, "
        "f.flag, f.reason_code FROM mart.fct_contribution_profit p "
        "JOIN mart.fct_flags f ON f.month = p.month AND f.account_id = p.account_id "
        "JOIN (SELECT DISTINCT account_id, display_name FROM raw.accounts) a ON a.account_id = p.account_id "
        "WHERE p.month = ? ORDER BY p.account_id", [f"{month}-01"],
    ).fetchall()
    keys = ("account_id", "display_name", "revenue_cents", "profit_cents", "flag", "reason_code")
    return [dict(zip(keys, r)) for r in rows]


def _allocation(con: duckdb.DuckDBPyConnection, month: str) -> tuple[str, list[str]]:
    """The recommended rule from the allocation file the month was closed with, and every rule."""
    row = con.execute("SELECT content FROM raw.ref_config WHERE name = 'allocation'").fetchone()
    recommended = (yaml.safe_load(row[0]) or {}).get("recommended") if row else None
    if not recommended:
        raise MarginError(EXIT_PRECONDITION, "the warehouse has no recorded allocation rule: run close",
                          month=month)
    rules = [r[0] for r in con.execute(
        "SELECT DISTINCT rule FROM mart.fct_contribution_by_rule WHERE month = ? ORDER BY rule",
        [f"{month}-01"]).fetchall()]
    return recommended, rules


def _scenario(month: str, accounts: list[dict], registry: dict[str, dict]) -> dict:
    """The scenario section's data: the list-price trend of each tier at the series' first and last
    period, cited from its `ref/` record, and for each case the firm and soft accounts. IDs and labels."""
    first, last = scenario.PERIODS[0], scenario.PERIODS[-1]
    trend = []
    for tier in scenario.TIERS:
        ref = f"ref/list_price_series/{tier}"
        rows = {r["period"]: r for r in json.loads(_record(registry, ref)["value_text"])}
        trend.append({
            "tier": tier, "ref": ref,
            **{key: {"price": f"{scenario.tier_price(rows[period]):.4f}", "source_url": rows[period]["source_url"],
                     "retrieved_on": rows[period]["retrieved_on"]} for key, period in (("first", first), ("last", last))},
        })
    return {
        "first": first, "last": last, "trend": trend,
        "cases": [
            {"case": case, "firm": [a for a in accounts if a["flag"] == "firm"],
             "soft": [a for a in accounts if a["flag"] == "soft"]}
            for case in scenario.CASES
        ],
        "quarters": [f"Q{q}" for q in range(1, len(scenario.QUARTER_END_K) + 1)],
    }


# The measured build-cost totals the section lists, in order: (measure, label, unit shown after the figure).
BUILD_MEASURES = (
    ("input_tokens", "Model input tokens", ""),
    ("output_tokens", "Model output tokens", ""),
    ("elapsed_hours", "Elapsed time", " hours"),
    ("review_minutes", "Author review time", " minutes"),
)
VENDOR_PREFIX = "bvb/vendor_annual_cost/"


def _reference_row(registry: dict[str, dict], number_id: str) -> dict:
    """The one reference-file row a `ref/` record holds, as text."""
    return json.loads(_record(registry, number_id)["value_text"])[0]


def _plain(amount: str) -> str:
    """A reference amount as it is read: grouped thousands, no trailing zero decimals."""
    value = Decimal(amount)
    return f"{value.quantize(Decimal(1)):,}" if value == value.to_integral_value() else f"{value:,}"


def _build_vs_buy(registry: dict[str, dict]) -> dict:
    """The build-versus-buy section's data. The section exists once the build-cost totals have been
    collected. A vendor is bought when its annual list price is below the build cost amortised over
    the year, plus the year's run cost, plus the maintenance allowance; otherwise it is built."""
    if "bvb/build_cost" not in registry:
        return {"bvb_present": False, "bvb_measures": [], "bvb_vendors": []}
    threshold = sum(registry[f"bvb/{item}"]["value_cents"]
                    for item in ("build_cost_amortised", "run_cost_annual", "maintenance_allowance"))
    measures = []
    for measure, label, unit in BUILD_MEASURES:
        ref = f"ref/build_cost/{measure}"
        if ref in registry:
            row = _reference_row(registry, ref)
            measures.append({"label": label, "ref": ref, "shown": _plain(row["value"]) + unit,
                             "source": row["source"]})
    run_ref = "ref/build_cost/run_cost_monthly_usd"
    run = _reference_row(registry, run_ref)
    build_source = _reference_row(registry, "ref/build_cost/build_cost_usd")["source"]
    vendors = []
    for number_id in sorted(i for i in registry if i.startswith(VENDOR_PREFIX)):
        vendor = number_id[len(VENDOR_PREFIX):]
        ref = f"ref/vendor_prices/{vendor}"
        row = _reference_row(registry, ref)
        vendors.append({
            "id": number_id, "vendor": vendor, "product": row["product"], "ref": ref,
            "verdict": "buy" if registry[number_id]["value_cents"] < threshold else "build",
            "source_url": row["source_url"], "retrieved_on": row["retrieved_on"],
        })
    return {"bvb_present": True, "bvb_measures": measures, "bvb_vendors": vendors,
            "bvb_run_ref": run_ref, "bvb_run_monthly": "$" + _plain(run["value"]), "bvb_run_source": run["source"],
            "bvb_build_source": build_source}


def _controls(con: duckdb.DuckDBPyConnection, month: str) -> list[str]:
    """The control IDs of the month's latest run: the run that closed it, else the last one recorded."""
    d = f"{month}-01"
    for query in (
        "SELECT control_id FROM ctl.control_results WHERE month = ? AND run_id = "
        "(SELECT closed_run_id FROM state.close_state WHERE month = ?) ORDER BY control_id",
        "SELECT control_id FROM ctl.control_results WHERE month = ? AND run_id = "
        "(SELECT run_id FROM ctl.control_results WHERE month = ? ORDER BY rowid DESC LIMIT 1) ORDER BY control_id",
    ):
        names = [r[0] for r in con.execute(query, [d, d]).fetchall()]
        if names:
            return names
    return []


def gather(con: duckdb.DuckDBPyConnection, month: str, registry: dict[str, dict]) -> tuple[list[dict], dict]:
    """Every account of the month and the data of each section: IDs and labels, never values."""
    accounts = _accounts(con, month)
    by_profit = sorted(accounts, key=lambda a: (-a["profit_cents"], a["account_id"]))
    top = by_profit[:RANKED]  # the bottom list takes what is left, so a short book is listed once
    bottom = by_profit[len(top):][-RANKED:]
    p25 = registry.get(f"rollup/{month}/revenue_p25", {}).get("value_cents")
    firm = [a for a in accounts if a["flag"] == "firm"]
    flagged = [a for a in accounts if a["flag"] != "none"]
    recommended, rules = _allocation(con, month)
    below: dict[tuple[str, str], bool] = {}
    for rule, account_id, is_below in con.execute(
        "SELECT rule, account_id, below_floor FROM mart.fct_contribution_by_rule WHERE month = ?",
        [f"{month}-01"],
    ).fetchall():
        below[(rule, account_id)] = is_below
    sections = {
        "top": top,
        "bottom": sorted(bottom, key=lambda a: (a["profit_cents"], a["account_id"])),
        "firm": firm,
        "soft": [a for a in accounts if a["flag"] == "soft"],
        "zero_revenue": [a for a in accounts if a["revenue_cents"] == 0],
        "actions": [{**a, "action": key, "action_text": text} for a, (key, text) in zip(
            firm, (action_for(a["reason_code"], a["revenue_cents"], p25) for a in firm))],
        "rules": rules,
        "recommended": recommended,
        "spread": flagged,
        "changes": [
            {"rule": rule, "accounts": [a["account_id"] for a in accounts
                                        if below[(rule, a["account_id"])] != below[(recommended, a["account_id"])]]}
            for rule in rules if rule != recommended
        ],
        "controls": _controls(con, month),
        "scenario": _scenario(month, accounts, registry),
        **_build_vs_buy(registry),
    }
    return accounts, sections


def _shown(node: dict) -> str:
    """The value of a node as the explain page prints it."""
    if node.get("value_cents") is not None:
        return f"{format_cents(node['value_cents'])} ({node['value_cents']} cents)" \
            if node.get("unit") == "cents" else str(node["value_cents"])
    return str(node.get("value_text") or "")


def explain_page(node: dict) -> str:
    """The explain page of one node of an `explain` tree: the first appearance of an ID, so it
    carries its rows or its children. Digests are recomputed here from what the node holds."""
    number_id = node["number_id"]
    rows = sorted(node.get("rows", []), key=lambda r: r["_row_hash"])
    children = node.get("children")
    if children is not None:
        recomputed = prov.digest_children([(c["number_id"], c["input_digest"]) for c in children])
    else:
        recomputed = prov.digest_rows([r["_row_hash"] for r in node.get("rows", [])])
    root = "../" * (number_id.count("/") + 1)
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                      undefined=StrictUndefined)
    return env.get_template("explain.html.j2").render(
        node=node, value=_shown(node), recomputed=recomputed, root=root,
        verified=recomputed == node["input_digest"] and bool(node.get("digest_verified")),
        shown_rows=[{**r, "row": json.dumps(r["row"], sort_keys=True)} for r in rows[:EXPLAIN_ROWS]],
        more_rows=max(len(rows) - EXPLAIN_ROWS, 0),
        children=[c["number_id"] for c in children or []],
        memo=f"{root}{node['month']}/index.html" if node.get("month") else None,
    )


def _pages(con: duckdb.DuckDBPyConnection, month: str, ids: set[str]) -> dict[str, str]:
    """An explain page for every ID in `ids` and every ID beneath them, by number ID. A composite
    is walked first: its tree holds the pages of its descendants, which are then not walked again."""
    composites = {r[0] for r in con.execute(
        "SELECT number_id FROM prov.records WHERE month = ? AND starts_with(source_query, '[')",
        [f"{month}-01"]).fetchall()}
    pages: dict[str, str] = {}

    def collect(node: dict) -> None:
        if node.get("repeated") or node.get("truncated") or node["number_id"] in pages:
            return
        pages[node["number_id"]] = explain_page(node)
        for child in node.get("children", []):
            collect(child)

    for number_id in sorted(ids, key=lambda i: (i not in composites, i)):
        if number_id not in pages:
            collect(explain_mod.explain(con, number_id))
    return pages


def _compiled_sql(con: duckdb.DuckDBPyConnection) -> str:
    try:
        row = con.execute("SELECT sql FROM state.model_sql WHERE model = ?", [PUBLISHED_MODEL]).fetchone()
    except duckdb.CatalogException:
        row = None
    if row is None:
        raise MarginError(EXIT_PRECONDITION, "compiled SQL not stored: run close")
    return row[0]


def values_csv(registry: dict[str, dict], month: str, accounts: list[dict]) -> str:
    """`number_id,value` for every ID of the agreement set: each account's six cents components and
    margin as the exact four-decimal string (or `n/a`), then the six month totals."""
    ids = [f"cp/{month}/{a['account_id']}/{c}" for a in accounts for c in (*prov.LEAF_COMPONENTS, "contribution_profit", "margin_pct")]
    ids += [f"rollup/{month}/{m}" for m in prov.ROLLUP_SUMS]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["number_id", "value"])
    for number_id in ids:
        rec = registry[number_id]
        writer.writerow([number_id, rec["value_text"] if rec["value_cents"] is None else rec["value_cents"]])
    return out.getvalue()


def _built_months(out: Path) -> list[str]:
    return sorted(d.name for d in out.iterdir() if MONTH_DIR.fullmatch(d.name) and (d / "index.html").is_file())


def llms_txt(months: list[str]) -> str:
    lines = [f"# {PUBLISHED_MODEL}", "", f"owner: {OWNER}", f"definition: {DEFINITION}", "",
             f"- [sql](sql/{PUBLISHED_MODEL}.sql)"]
    lines += [f"- [values {m}]({m}/values.csv)" for m in months]
    return "\n".join(lines) + "\n"


def index_html(months: list[str]) -> str:
    items = "".join(f'<li><a href="{escape(m)}/">{escape(m)}</a></li>' for m in months)
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Acme AI memos</title>'
            f'</head><body><h1>Acme AI memos</h1><ul>{items}</ul>'
            '<p><a href="llms.txt">llms.txt</a></p></body></html>\n')


def _write(path: Path, text: str, written: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(path)


def build(con: duckdb.DuckDBPyConnection, month: str, out: Path) -> list[Path]:
    """Render the month's page and its explain pages, values and the site files into `out`;
    return the files written. The root index and `llms.txt` list the months found in `out`."""
    (scenario_records,) = con.execute(
        "SELECT count(*) FROM prov.records WHERE starts_with(number_id, ?)", [f"scenario/{month}/"]).fetchone()
    if not scenario_records:
        raise MarginError(EXIT_PRECONDITION, f"scenario not run for month {month}", month=month)
    sql = _compiled_sql(con)
    registry = _registry(con, month)
    accounts, sections = gather(con, month, registry)
    html = render(month, accounts, registry, sections)
    pages = _pages(con, month, set(NUM_ID.findall(html)) | set(CITE_ID.findall(html)))
    written: list[Path] = []
    for number_id, text in pages.items():
        _write(out / "explain" / f"{number_id}.html", text, written)
    _write(out / "sql" / f"{PUBLISHED_MODEL}.sql", sql, written)
    _write(out / month / "values.csv", values_csv(registry, month, accounts), written)
    _write(out / month / "index.html", html, written)
    months = _built_months(out)
    _write(out / "llms.txt", llms_txt(months), written)
    _write(out / "index.html", index_html(months), written)
    return written
