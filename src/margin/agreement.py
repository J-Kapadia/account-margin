"""`agreement`: one metric, one answer, wherever it is read.

For a closed month the agreement set is every cents component and `margin_pct` of every account in
`mart.fct_contribution_profit` (seven IDs per account) plus the month's six totals. Each ID is asked
through four interfaces and the values must be identical:

- `cli`: the real `explain` command, invoked in-process for every ID and as a subprocess for a
  seeded sample of IDs;
- `sql`: the mart itself (totals as sums);
- `memo`: the `data-value` of the `data-num-id` span in `<site>/<month>/index.html`;
- `values`: `<site>/<month>/values.csv`, reached through the link `values <month>` in `llms.txt`.

Cents compare as integers, `margin_pct` as its four-decimal string or `n/a`. The command is a reader:
it opens the warehouse read-only and writes only the result file when asked. A missing or differing
ID is `CHK-AGREEMENT` (exit 3), naming the ID and two interfaces that disagree. This module never
imports the `cli` module (which reaches the generator's reference implementation); the `explain`
command is handed in.
"""

from __future__ import annotations

import contextlib
import csv
import html.parser
import io
import json
import random
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import click
import duckdb

from . import prov
from .errors import EXIT_CONTROL, MarginError
from .paths import Paths
from .warehouse import connect

CHECK = "CHK-AGREEMENT"
INTERFACES = ["cli", "sql", "memo", "values"]
# The interface whose value the others are held against first: the mart is the one definition.
PRECEDENCE = ("sql", "cli", "memo", "values")
SAMPLE_SEED = 20261013
SAMPLE_SIZE = 20
CELL_COMPONENTS = ("revenue", "inference_cost", "cloud_direct", "cloud_shared", "support_cost",
                   "contribution_profit")

LLMS_LINE = re.compile(
    r"# \S.*|owner: \S.*|definition: \S.*|- \[(?P<text>[^\[\]]+)\]\((?P<href>[^()\s]+)\)"
)


def _fail(message: str, month: str | None, detail: list[str] | None = None, rows: int = 1) -> MarginError:
    return MarginError(
        EXIT_CONTROL, f"{CHECK}: {message}", month=month,
        failed_controls=[{"id": CHECK, "violating_rows": rows, "detail": detail or [message]}],
    )


def agreement_set(con: duckdb.DuckDBPyConnection, month: str) -> list[str]:
    """Every ID of the month's agreement set, in a fixed order."""
    accounts = [r[0] for r in con.execute(
        "SELECT account_id FROM mart.fct_contribution_profit WHERE month = ? ORDER BY account_id",
        [f"{month}-01"]).fetchall()]
    ids = [f"cp/{month}/{a}/{c}" for a in accounts for c in (*CELL_COMPONENTS, "margin_pct")]
    return ids + [f"rollup/{month}/{m}" for m in prov.ROLLUP_SUMS]


# ------------------------------------------------------------------ readers


def read_sql(con: duckdb.DuckDBPyConnection, month: str) -> dict[str, str]:
    columns = ", ".join(f"{c}_cents" for c in CELL_COMPONENTS)
    out: dict[str, str] = {}
    for account, *cells, margin in con.execute(
        f"SELECT account_id, {columns}, COALESCE(CAST(margin_pct AS VARCHAR), 'n/a') "
        "FROM mart.fct_contribution_profit WHERE month = ? ORDER BY account_id", [f"{month}-01"],
    ).fetchall():
        for component, cents in zip(CELL_COMPONENTS, cells):
            out[f"cp/{month}/{account}/{component}"] = str(cents)
        out[f"cp/{month}/{account}/margin_pct"] = margin
    sums = ", ".join(f"CAST(sum({c}_cents) AS BIGINT)" for c in CELL_COMPONENTS)
    totals = con.execute(f"SELECT {sums} FROM mart.fct_contribution_profit WHERE month = ?",
                         [f"{month}-01"]).fetchone()
    for component, total in zip(CELL_COMPONENTS, totals):
        if total is not None:
            out[f"rollup/{month}/{component}"] = str(total)
    return out


def _value_of(chain: dict) -> str:
    return str(chain["value_cents"]) if chain.get("value_cents") is not None else str(chain.get("value_text"))


def read_cli(warehouse: Path, ids: list[str], explain_command: click.Command) -> dict[str, str]:
    """The `explain` command invoked in-process for every ID; an ID it cannot answer is absent."""
    out: dict[str, str] = {}
    for number_id in ids:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                explain_command.main(
                    args=[number_id, "--depth", "0", "--json", "--warehouse", str(warehouse)],
                    prog_name="margin explain", standalone_mode=False)
            except (SystemExit, click.ClickException):
                continue
        out[number_id] = _value_of(json.loads(stdout.getvalue())["chain"])
    return out


def sample_ids(ids: list[str]) -> list[str]:
    return random.Random(SAMPLE_SEED).sample(ids, min(SAMPLE_SIZE, len(ids)))


def read_cli_subprocess(warehouse: Path, ids: list[str]) -> dict[str, str]:
    """`margin explain` as a real process for each of `ids`; an ID it cannot answer is absent."""
    out: dict[str, str] = {}
    for number_id in ids:
        proc = subprocess.run(
            [sys.executable, "-m", "margin", "explain", number_id, "--depth", "0", "--json",
             "--warehouse", str(warehouse)],
            capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            out[number_id] = _value_of(json.loads(proc.stdout)["chain"])
    return out


def _merge(pairs, into: dict[str, str]) -> None:
    """Add (id, value) pairs; an ID that appears with different values keeps all of them, so it differs."""
    for number_id, value in pairs:
        old = into.get(number_id)
        if old is None:
            into[number_id] = value
        elif value not in old.split(" | "):
            into[number_id] = f"{old} | {value}"


class _NumSpans(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.pairs: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-num-id" in a and a.get("data-value") is not None:
            self.pairs.append((a["data-num-id"], a["data-value"]))


def read_memo(page: Path) -> dict[str, str]:
    parser = _NumSpans()
    parser.feed(page.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    _merge(parser.pairs, out)
    return out


def read_values(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows or rows[0] != ["number_id", "value"]:
        raise _fail(f"{path.name} does not start with the header number_id,value", None)
    out: dict[str, str] = {}
    for row in rows[1:]:
        if len(row) != 2:
            raise _fail(f"{path.name} has a row that is not number_id,value: {row!r}", None)
        _merge([(row[0], row[1])], out)
    return out


def parse_llms(text: str) -> dict[str, str]:
    """Link text to href. Every line is blank, a heading, an `owner:` or `definition:` line, or a
    `- [text](href)` link; anything else, or a link text used twice, is a malformed file."""
    links: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        found = LLMS_LINE.fullmatch(line)
        if found is None:
            raise _fail(f"llms.txt line {number} is not part of the format: {line!r}", None)
        if found["text"] is not None:
            if found["text"] in links:
                raise _fail(f"llms.txt links {found['text']!r} twice", None)
            links[found["text"]] = found["href"]
    return links


def values_path(site: Path, month: str) -> Path:
    llms = site / "llms.txt"
    if not llms.is_file():
        raise _fail(f"no llms.txt in {site}", month)
    links = parse_llms(llms.read_text(encoding="utf-8"))
    href = links.get(f"values {month}")
    if href is None:
        raise _fail(f"llms.txt has no link 'values {month}'", month)
    return site / href


# ------------------------------------------------------------------ comparison


def compare(readings: dict[str, dict[str, str]], ids: list[str] | None = None) -> list[dict]:
    """One problem for each ID that is missing from an interface or differs between two. The first
    interface (in precedence order) that holds the ID is the one the others are held against."""
    names = [n for n in PRECEDENCE if n in readings] + [n for n in readings if n not in PRECEDENCE]
    if ids is None:
        ids = sorted(set().union(*readings.values()))
    problems = []
    for number_id in ids:
        held = [n for n in names if number_id in readings[n]]
        base = held[0] if held else names[0]
        base_value = readings[base].get(number_id)
        for name in names:
            if name == base:
                continue
            value = readings[name].get(number_id)
            if value != base_value:
                problems.append({"id": number_id, "a": base, "a_value": base_value,
                                 "b": name, "b_value": value})
                break
    return problems


def describe(problem: dict) -> str:
    shown = lambda v: "missing" if v is None else repr(v)  # noqa: E731
    return (f"{problem['id']}: {problem['a']} has {shown(problem['a_value'])}, "
            f"{problem['b']} has {shown(problem['b_value'])}")


def rate(problems: list[dict], total: int) -> str:
    bad = len({p["id"] for p in problems})
    if not bad:
        return "0"
    return format((Decimal(bad) / Decimal(total)).quantize(Decimal("0.000001")), "f")


# ------------------------------------------------------------------ the run


def run(paths: Paths, month: str, site: Path, values: Path | None, write: bool, *,
        explain_command: click.Command, check: bool = True) -> dict:
    """Ask every ID of the month's agreement set through every interface. With `check`, a
    disagreement raises `CHK-AGREEMENT`; without it the rate is reported. `values` replaces the
    file `llms.txt` points to."""
    con = connect(paths.warehouse, read_only=True)
    try:
        ids = agreement_set(con, month)
        sql = read_sql(con, month)
    finally:
        con.close()
    memo = read_memo(site / month / "index.html")
    values_file = values or values_path(site, month)
    if not values_file.is_file():
        raise _fail(f"no values file at {values_file}", month)
    cached = read_values(values_file)
    cli = read_cli(paths.warehouse, ids, explain_command)
    sampled = sample_ids(ids)
    problems = compare({"cli": cli, "sql": sql, "memo": memo, "values": cached}, ids)
    problems += compare({"cli": {i: cli[i] for i in sampled if i in cli},
                         "cli (subprocess)": read_cli_subprocess(paths.warehouse, sampled)}, sampled)
    result = {"ids_checked": len(ids), "interfaces": list(INTERFACES),
              "disagreement_rate": rate(problems, len(ids)), "sampled_subprocess": len(sampled)}
    if write:
        target = site / month / "agreement.json"
        target.write_text(json.dumps({k: result[k] for k in ("ids_checked", "interfaces", "disagreement_rate")},
                                     indent=2) + "\n", encoding="utf-8")
    if problems and check:
        detail = [describe(p) for p in problems]
        raise _fail(f"{len(detail)} of {len(ids)} IDs disagree (rate {result['disagreement_rate']}); {detail[0]}",
                    month, detail, rows=len(detail))
    return result
