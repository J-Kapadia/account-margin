"""`memo` (plan §6.1, §14.3): a Jinja2 page whose every number goes through `num(id)`.

The memo is a reader: it opens the warehouse read-only and writes nothing to it.
`num` renders an ID found in `prov.records` as a span with `data-num-id` and the
unrounded `data-value`, and fails the build (`CHK-MEMO-NUM`, exit 3) for an ID with no record.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape

from . import prov
from .errors import EXIT_CONTROL, MarginError
from .money import format_cents

TEMPLATES = Path(__file__).resolve().parent / "templates"


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

    def num(number_id: str) -> Markup:
        rec = registry.get(number_id)
        if rec is None:
            raise MarginError(
                EXIT_CONTROL, f"memo uses {number_id}, which has no provenance record",
                failed_controls=[{"id": "CHK-MEMO-NUM", "violating_rows": 1, "detail": [number_id]}],
            )
        value = rec["value_cents"] if rec["value_cents"] is not None else rec["value_text"]
        shown = format_cents(rec["value_cents"]) if rec["unit"] == "cents" else rec["value_text"]
        return Markup('<span data-num-id="{}" data-value="{}">{}</span>').format(
            number_id, str(value), shown
        )

    env.filters["num"] = num
    return env


def render(month: str, accounts: list[dict], registry: dict[str, dict]) -> str:
    template = make_env(registry).get_template("memo.html.j2")
    return template.render(month=month, accounts=accounts, gl_codes=prov.GL_CODES)


def build(con: duckdb.DuckDBPyConnection, month: str, out: Path) -> list[Path]:
    """Render the month's page and a root index into `out`; return the files written."""
    accounts = [
        {"account_id": a, "display_name": n}
        for a, n in con.execute(
            "SELECT a.account_id, a.display_name FROM raw.accounts a "
            "WHERE a.account_id IN (SELECT account_id FROM mart.fct_contribution_profit "
            "WHERE month = ?) ORDER BY a.account_id", [f"{month}-01"],
        ).fetchall()
    ]
    html = render(month, accounts, _registry(con, month))
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
