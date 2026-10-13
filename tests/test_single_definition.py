"""Lint: the contribution formula, the flag predicate and half-even rounding are each defined once.

contribution profit is defined in exactly one place, the dbt macro `contribution_cents()`,
with one allowlisted file in `src/`, the generator's independent Python reference. The predicate
and `margin_pct` are single macros too, so a second copy in a model or in Python fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCANNED = [(ROOT / "dbt", {".sql", ".yml"}), (ROOT / "src", {".py", ".j2", ".sql"})]

# revenue less a cost, or one cost less the next: the subtraction pattern of the profit formula
FORMULA = re.compile(
    r"revenue\w*\s*-\s*\(?\s*(?:\w+\.)?\w*(?:inference|cloud|support)"
    r"|\{\{\s*revenue\s*\}\}\s*-\s*\{\{"
    r"|(?:inference|cloud_direct)\w*\s*-\s*\(?\s*(?:\w+\.)?\w*(?:cloud|support)",
    re.IGNORECASE,
)
# half-even rounding and the integer floor test, which belong to margin_pct_4dp / is_below
ROUNDING = re.compile(r"round_even|ROUND_EVEN|\*\s*100\s*<", re.IGNORECASE)

FORMULA_HOMES = {"dbt/macros/contribution_cents.sql", "src/margin/truth_reference.py"}
ROUNDING_HOMES = {"dbt/macros/margin.sql", "src/margin/truth_reference.py"}


def _files():
    for base, suffixes in SCANNED:
        for path in sorted(base.rglob("*")):
            parts = path.relative_to(ROOT).parts
            if path.is_file() and path.suffix in suffixes and "target" not in parts and "logs" not in parts:
                yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _offenders(pattern: re.Pattern, homes: set[str]) -> list[str]:
    return [name for name, text in _files() if name not in homes and pattern.search(text)]


def test_no_contribution_formula_outside_the_macro():
    assert _offenders(FORMULA, FORMULA_HOMES) == []


def test_no_rounding_or_floor_test_outside_the_margin_macros():
    assert _offenders(ROUNDING, ROUNDING_HOMES) == []


def test_the_lint_finds_the_formula_where_it_lives():
    # Positive controls: the patterns match the real definitions, so a clean scan means something.
    names = {name for name, text in _files() if FORMULA.search(text)}
    assert FORMULA_HOMES <= names
    assert {n for n, t in _files() if ROUNDING.search(t)} >= {"dbt/macros/margin.sql"}


@pytest.mark.parametrize(
    "snippet",
    [
        "select revenue_cents - inference_cost_cents - cloud_direct_cents from t",
        "p.revenue_cents - (p.inference_cost_cents + 1)",
        "inference_cost_cents - cloud_shared_cents",
        "profit = revenue - support",
        "where profit_cents * 100 < 30 * revenue_cents",
        "select round_even(x, 4)",
    ],
)
def test_the_patterns_catch_a_second_copy(snippet):
    assert FORMULA.search(snippet) or ROUNDING.search(snippet)


def test_models_call_the_macro_and_the_profit_mart_has_no_arithmetic():
    by_rule = (ROOT / "dbt/models/marts/fct_contribution_by_rule.sql").read_text()
    profit = (ROOT / "dbt/models/marts/fct_contribution_profit.sql").read_text()
    assert "contribution_cents(" in by_rule
    assert "contribution_cents(" not in profit
    body = re.sub(r"--[^\n]*", "", profit)
    assert not re.search(r"[-+*/]", re.sub(r"'[^']*'|\"[^\"]*\"|\{\{.*?\}\}|\{%.*?%\}", "", body))
    # no other model calls the macro
    callers = [p.name for p in (ROOT / "dbt/models").rglob("*.sql") if "contribution_cents(" in p.read_text()]
    assert callers == ["fct_contribution_by_rule.sql"]
