"""Integer-cent helpers. Money is never a float (plan §3)."""

from __future__ import annotations


def format_cents(cents: int) -> str:
    """Render integer cents as `$1,234.56` (`-$0.05` for negatives)."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{frac:02d}"
