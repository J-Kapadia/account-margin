"""`close --check` (step 7): compare a computed month with the answer key in `truth/`.

Read-only. The mart is compared with the key per account (the five cost components and the
profit under every allowed rule), the firm and soft flag sets with the planted accounts and the
decoys, the reason codes with the planted reasons, and each account's per-rule `below` result,
its flag and the status-change counts with `rule_flags.csv`. Provenance completeness (every
mart number has a record with a digest and a source query) is checked last and exits 6.

Failures name identifiers (number IDs, account IDs) and never print what the key holds.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb

from . import prov
from .errors import EXIT_CHECK, EXIT_PRECONDITION, EXIT_PROVENANCE, MarginError
from .gen_config import Config
from .warehouse import month_date

TRUTH_FILES = ("cost_components.csv", "planted_flags.json", "rule_flags.csv")
CHK_COMPONENTS = "CHK-COMPONENTS"
CHK_FLAGS = "CHK-FLAGS"
CHK_REASONS = "CHK-REASONS"
CHK_RULE_FLAGS = "CHK-RULE-FLAGS"
CHK_PROVENANCE = "CHK-PROVENANCE"
ORDER = (CHK_COMPONENTS, CHK_FLAGS, CHK_REASONS, CHK_RULE_FLAGS, CHK_PROVENANCE)
# The signed rule's cost components, as the key names them.
COMPONENT_COLUMNS = {
    "revenue": "revenue_cents",
    "inference_cost": "inference_cost_cents",
    "cloud_direct": "cloud_direct_cents",
    "support_cost": "support_cost_cents",
}


def require_truth(truth: Path, *, month: str | None = None) -> None:
    """Exit 4 when the answer key is absent or incomplete (EC-01)."""
    if not truth.is_dir():
        raise MarginError(EXIT_PRECONDITION, "truth/ not found: --check needs the answer key", month=month)
    missing = [name for name in TRUTH_FILES if not (truth / name).is_file()]
    if missing:
        raise MarginError(
            EXIT_PRECONDITION, f"truth/ is incomplete: missing {', '.join(missing)}", month=month
        )


def _rows(path: Path, month: str) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [r for r in csv.DictReader(fh) if r["month"] == month]


def _by_account(rows: list[dict]) -> dict[str, dict]:
    return {r["account_id"]: r for r in rows}


def _compare_accounts(
    expected: dict[str, dict], got: dict[str, dict], month: str, id_of, fields
) -> list[str]:
    """Number IDs where an account is missing or extra, or a field differs. `fields` yields
    (field name, id) pairs for an account."""
    bad: list[str] = []
    for account in sorted(set(expected) | set(got)):
        if account not in expected or account not in got:
            bad.append(id_of(account))
            continue
        for name, number_id in fields(account):
            if str(expected[account][name]) != str(got[account][name]):
                bad.append(number_id)
    return sorted(set(bad))


def components(con, month: str, truth: Path, rules: list[str], signed: str) -> list[str]:
    d = month_date(month)
    key = _by_account(_rows(truth / "cost_components.csv", month))
    bad: list[str] = []
    mart = {
        (rule, a): dict(zip(("revenue", "inference_cost", "cloud_direct", "support_cost", "shared", "profit"), v))
        for rule, a, *v in con.execute(
            "SELECT rule, account_id, revenue_cents, inference_cost_cents, cloud_direct_cents, "
            "support_cost_cents, cloud_shared_cents, contribution_profit_cents "
            "FROM mart.fct_contribution_by_rule WHERE month = ?", [d],
        ).fetchall()
    }
    for rule in rules:
        got = {a: v for (r, a), v in mart.items() if r == rule}
        expected = {
            a: {
                **{c: int(row[col]) for c, col in COMPONENT_COLUMNS.items()},
                "shared": int(row[f"cloud_shared_{rule}_cents"]),
                "profit": int(row[f"contribution_profit_{rule}_cents"]),
            }
            for a, row in key.items()
        }

        def ids(account: str, rule=rule):
            if rule == signed:
                return [(c, f"cp/{month}/{account}/{c}") for c in COMPONENT_COLUMNS] + [
                    ("shared", f"cp/{month}/{account}/cloud_shared"),
                    ("profit", f"cp/{month}/{account}/contribution_profit"),
                ]
            # Under a rule that is not in force only the allocation and the profit vary.
            return [("shared", f"cpr/{month}/{account}/{rule}"), ("profit", f"cpr/{month}/{account}/{rule}")]

        bad += _compare_accounts(
            expected, got, month,
            lambda account: f"cp/{month}/{account}/contribution_profit", ids,
        )
    return sorted(set(bad))


def flags(con, month: str, truth: Path) -> tuple[list[str], list[str]]:
    """(flag-set mismatches, reason mismatches) as flag IDs."""
    planted = json.loads((truth / "planted_flags.json").read_text(encoding="utf-8"))
    firm_key = {p["account_id"]: p["reason_code"] for p in planted.get(month, [])}
    soft_key = set(planted.get("decoys", {}).get(month, []))
    got = con.execute(
        "SELECT account_id, flag, reason_code FROM mart.fct_flags WHERE month = ?", [month_date(month)]
    ).fetchall()
    firm = {a: r for a, f, r in got if f == "firm"}
    soft = {a for a, f, _ in got if f == "soft"}
    wrong_set = sorted(
        {f"flag/{month}/{a}" for a in set(firm) ^ set(firm_key)}
        | {f"flag/{month}/{a}" for a in soft ^ soft_key}
    )
    wrong_reason = sorted(f"flag/{month}/{a}" for a in set(firm) & set(firm_key) if firm[a] != firm_key[a])
    return wrong_set, wrong_reason


def rule_flags(con, month: str, truth: Path, rules: list[str], signed: str, records: list[dict]) -> list[str]:
    key = _by_account(_rows(truth / "rule_flags.csv", month))
    d = month_date(month)
    below = {
        (rule, a): bool(b)
        for rule, a, b in con.execute(
            "SELECT rule, account_id, below_floor FROM mart.fct_contribution_by_rule WHERE month = ?", [d]
        ).fetchall()
    }
    flag_rows = {a: (f, n) for a, f, n in con.execute(
        "SELECT account_id, flag, rules_below_floor FROM mart.fct_flags WHERE month = ?", [d]).fetchall()}
    bad: list[str] = []
    for account in sorted(set(key) | {a for _, a in below}):
        if account not in key or (signed, account) not in below:
            bad.append(f"flag/{month}/{account}")
            continue
        for rule in rules:
            if (key[account][f"below_{rule}"] == "true") != below.get((rule, account)):
                bad.append(f"cpr/{month}/{account}/{rule}")
        flag, n_below = flag_rows[account]
        if flag != key[account]["flag"] or str(n_below) != key[account]["rules_below_floor"]:
            bad.append(f"flag/{month}/{account}")
    by_id = {r["number_id"]: r for r in records}
    for rule in rules:
        expected = sum(
            1 for row in key.values()
            if (row[f"below_{rule}"] == "true") != (row[f"below_{signed}"] == "true")
        )
        number_id = f"rollup/{month}/accounts_status_change_{rule}"
        if number_id not in by_id or by_id[number_id]["value_cents"] != expected:
            bad.append(number_id)
    return sorted(set(bad))


def provenance_gaps(con, month: str, records: list[dict], config: Config) -> list[str]:
    """IDs of mart numbers with no record, or a record with no value, digest or source query."""
    d = month_date(month)
    rules = list(config.allocation.rules)
    accounts = [a for (a,) in con.execute(
        "SELECT account_id FROM mart.fct_contribution_profit WHERE month = ? ORDER BY 1", [d]).fetchall()]
    expected = [f"cp/{month}/{a}/{c}" for a in accounts for c in prov.COMPONENTS]
    expected += [f"flag/{month}/{a}" for a in accounts]
    expected += [f"cpr/{month}/{a}/{r}" for a in accounts for r in rules]
    expected += [f"pnl/{month}/{code}" for code in prov.ALL_GL_CODES]
    expected += [
        f"rollup/{month}/{m}"
        for m in (*prov.ROLLUP_SUMS, *prov.ROLLUP_COUNTS, *prov.ROLLUP_STATS,
                  *(f"accounts_status_change_{r}" for r in rules))
    ]
    expected += [f"cohort/{month}/{drv.code.lower()}_median" for drv in config.policy.drivers]
    by_id = {r["number_id"]: r for r in records}
    bad = []
    for number_id in expected:
        r = by_id.get(number_id)
        if (
            r is None or not r["input_digest"] or not r["source_query"]
            or (r["value_cents"] is None and r["value_text"] is None)
        ):
            bad.append(number_id)
    return sorted(bad)


def evaluate(
    con: duckdb.DuckDBPyConnection, month: str, truth: Path, config: Config, records: list[dict],
) -> list[dict]:
    """Every check's result, in a fixed order: id, status, violating_rows, detail (identifiers)."""
    rules, signed = list(config.allocation.rules), config.allocation.recommended
    wrong_set, wrong_reason = flags(con, month, truth)
    found = {
        CHK_COMPONENTS: components(con, month, truth, rules, signed),
        CHK_FLAGS: wrong_set,
        CHK_REASONS: wrong_reason,
        CHK_RULE_FLAGS: rule_flags(con, month, truth, rules, signed, records),
        CHK_PROVENANCE: provenance_gaps(con, month, records, config),
    }
    return [
        {"id": c, "status": "fail" if found[c] else "pass", "violating_rows": len(found[c]),
         "detail": [f"{number_id}: differs from the answer key" if c != CHK_PROVENANCE
                    else f"{number_id}: no complete provenance record" for number_id in found[c]]}
        for c in ORDER
    ]


def raise_for(month: str, results: list[dict], controls: list[dict]) -> None:
    """Exit 5 for an answer-key mismatch, else 6 for a provenance gap; nothing when all pass."""
    failed = [c for c in results if c["status"] == "fail"]
    if not failed:
        return
    truth_failed = [c for c in failed if c["id"] != CHK_PROVENANCE]
    code = EXIT_CHECK if truth_failed else EXIT_PROVENANCE
    raise MarginError(
        code, f"check failed for {month}: {', '.join(c['id'] for c in failed)}",
        month=month,
        controls=controls + [{k: c[k] for k in ("id", "status", "violating_rows")} for c in results],
        failed_controls=[
            {"id": c["id"], "violating_rows": c["violating_rows"], "detail": c["detail"]} for c in failed
        ],
    )
