from hypothesis import given, strategies as st

from margin.money import format_cents


def test_examples():
    assert format_cents(0) == "$0.00"
    assert format_cents(5) == "$0.05"
    assert format_cents(618985) == "$6,189.85"
    assert format_cents(-2500) == "-$25.00"


@given(st.integers(min_value=-10**12, max_value=10**12))
def test_round_trip(cents):
    text = format_cents(cents)
    sign = -1 if text.startswith("-") else 1
    dollars, frac = text.lstrip("-$").replace(",", "").split(".")
    assert sign * (int(dollars) * 100 + int(frac)) == cents
