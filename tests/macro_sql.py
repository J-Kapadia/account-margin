"""Render the dbt macros outside dbt, so a test can run their SQL in a bare DuckDB.

The macros build SQL strings and use no dbt context except `exceptions`, which is stubbed.
`tests.sql` holds dbt generic tests (`{% test %}` blocks) that plain Jinja does not know.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

MACROS = Path(__file__).resolve().parents[1] / "dbt" / "macros"


class _Exceptions:
    @staticmethod
    def raise_compiler_error(message: str):
        raise ValueError(message)


def _module():
    text = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(MACROS.glob("*.sql")) if p.name != "tests.sql"
    )
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.globals["exceptions"] = _Exceptions()
    return env.from_string(text).module


MODULE = _module()


def macro(name: str, *args) -> str:
    """The SQL a macro renders for `args`, whitespace squeezed."""
    return " ".join(str(getattr(MODULE, name)(*args)).split())
