"""`memo`: a Jinja2 page whose every number goes through `num(id)`.

The memo is a reader: it opens the warehouse read-only and writes nothing to it.
`num` renders an ID found in `prov.records` as a span with `data-num-id` and the
unrounded `data-value`, and fails the build (`CHK-MEMO-NUM`, exit 3) for an ID with no record.
`pct` is the same span with a percent sign after a figure. The queries here choose which
accounts and which rules a section shows; the template names the IDs and `num` supplies every value.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape

from . import prov
from .errors import EXIT_CONTROL, EXIT_PRECONDITION, MarginError
from .money import format_cents

TEMPLATES = Path(__file__).resolve().parent / "templates"
REPOSITORY = "https://github.com/J-Kapadia/account-margin"
# How many accounts the top and bottom tables show at most.
RANKED = 5
# The action for each firm-flagged account, by reason code: (machine key, text shown).
REPRICE_AT_RENEWAL = ("reprice_at_renewal", "Reprice at renewal")
MIGRATE = ("migrate_per_model_plan", "Migrate to a plan priced per model")
LET_CHURN = ("let_churn", "Let churn")
REPRICE = ("reprice", "Reprice")
REVIEW = ("review", "Review the account")
ACTIONS = {"DEEP_DISCOUNT": REPRICE_AT_RENEWAL, "DEDICATED_CLOUD": REPRICE_AT_RENEWAL, "MODEL_MIX": MIGRATE}


def _registry(con: duckdb.DuckDBPyConnection, month: str) -> dict[str, dict]:
    rows = con.execute(
        "SELECT number_id, value_cents, value_text, unit FROM prov.records WHERE month = ?",
        [f"{month}-01"],
    ).fetchall()
    return {r[0]: {"value_cents": r[1], "value_text": r[2], "unit": r[3]} for r in rows}


def make_env(registry: dict[str, dict]) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
    )

    def span(number_id: str, suffix: str = "") -> Markup:
        rec = registry.get(number_id)
        if rec is None:
            raise MarginError(
                EXIT_CONTROL, f"memo uses {number_id}, which has no provenance record",
                failed_controls=[{"id": "CHK-MEMO-NUM", "violating_rows": 1, "detail": [number_id]}],
            )
        cents = rec["value_cents"]
        value = cents if cents is not None else rec["value_text"]
        shown = format_cents(cents) if cents is not None and rec["unit"] == "cents" else rec["value_text"]
        if suffix and shown != "n/a":
            shown = f"{shown}{suffix}"
        return Markup('<span data-num-id="{}" data-value="{}">{}</span>').format(number_id, str(value), shown)

    env.filters["num"] = span
    env.filters["pct"] = lambda number_id: span(number_id, "%")
    return env


def empty_sections() -> dict:
    """The per-section data of a month with nothing in it; `render` fills what a caller leaves out."""
    return {
        "top": [], "bottom": [], "firm": [], "soft": [], "zero_revenue": [], "actions": [],
        "rules": [], "recommended": "", "spread": [], "changes": [], "controls": [],
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
    }
    return accounts, sections


def build(con: duckdb.DuckDBPyConnection, month: str, out: Path) -> list[Path]:
    """Render the month's page and a root index into `out`; return the files written."""
    registry = _registry(con, month)
    accounts, sections = gather(con, month, registry)
    html = render(month, accounts, registry, sections)
    page = out / month / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(html, encoding="utf-8")
    index = out / "index.html"
    index.write_text(
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Acme AI memos</title>'
        f'</head><body><h1>Acme AI memos</h1><ul><li><a href="{escape(month)}/">{escape(month)}</a>'
        "</li></ul></body></html>\n",
        encoding="utf-8",
    )
    return [index, page]
