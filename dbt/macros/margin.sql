{#- The flag predicate and the display margin, each defined once.
    is_below: strict, all integers, never a comparison of rounded percentages.
    margin_pct_4dp: contribution profit as a percent of revenue, DECIMAL(9,4) rounded half-even;
    NULL at zero revenue (rendered `n/a` by every interface). -#}
{% macro is_below(profit_cents, revenue_cents, floor_pct) -%}
    (({{ profit_cents }}) * 100 < cast({{ floor_pct }} as decimal(18, 6)) * ({{ revenue_cents }}))
{%- endmacro %}

{% macro margin_pct_4dp(profit_cents, revenue_cents) -%}
    cast(
        {{ half_even_div("cast((" ~ profit_cents ~ ") as hugeint) * 1000000", "cast((" ~ revenue_cents ~ ") as hugeint)") }}
        as decimal(18, 0)
    ) * cast(0.0001 as decimal(5, 4))
{%- endmacro %}
