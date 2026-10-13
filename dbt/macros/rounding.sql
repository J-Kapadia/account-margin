{#- Integer division rounded half-even. DuckDB's `/` returns a DOUBLE and its half-even rounding
    function rounds a DOUBLE, so neither is exact; this works on HUGEINT numerator and denominator and decides ties
    on the exact remainder. NULL when the denominator is zero. -#}
{% macro half_even_div(numerator, denominator) -%}
(
    case when {{ denominator }} = 0 then null else
        sign({{ numerator }}) * sign({{ denominator }}) * (
            (abs({{ numerator }}) // abs({{ denominator }}))
            + case
                when 2 * (abs({{ numerator }}) % abs({{ denominator }})) > abs({{ denominator }}) then 1
                when 2 * (abs({{ numerator }}) % abs({{ denominator }})) = abs({{ denominator }})
                     and (abs({{ numerator }}) // abs({{ denominator }})) % 2 = 1 then 1
                else 0
            end
        )
    end
)
{%- endmacro %}
