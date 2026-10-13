{#- Largest-remainder apportionment of pools to cents. The production implementation;
    the Python reference in src/margin/truth_reference.py is the independent one.

    pools:   a relation with the pool `keys` and `pool_cents` (BIGINT, not negative)
    weights: a relation with the pool `keys`, `account_id` and `weight` (integer)
    keys:    the list of columns that identify a pool, for example ['month', 'rule']

    Returns a SELECT of the `keys`, `account_id` and `allocated_cents`. Each share is floored,
    then the leftover cents go to the largest remainders, ties by ascending account_id, so every
    pool with an eligible weight sums exactly to its cents. Weights at or below zero are not
    eligible. A pool with cents and no eligible weight gets no rows: nothing is apportioned and
    there is no fallback to another rule; int_pool_violations reports it. -#}
{% macro apportion(pools, weights, keys) -%}
{%- set key_list = keys | join(', ') -%}
with eligible as (
    select {{ key_list }}, account_id, cast(weight as hugeint) as weight
    from {{ weights }}
    where weight > 0
),
totals as (
    select {{ key_list }}, sum(weight) as total_weight
    from eligible
    group by {{ key_list }}
),
shares as (
    select
        {% for k in keys %}w.{{ k }}, {% endfor %}w.account_id,
        (cast(p.pool_cents as hugeint) * w.weight) // t.total_weight as floor_cents,
        (cast(p.pool_cents as hugeint) * w.weight) % t.total_weight as remainder,
        p.pool_cents
    from eligible w
    join {{ pools }} p on {% for k in keys %}w.{{ k }} = p.{{ k }}{% if not loop.last %} and {% endif %}{% endfor %}
    join totals t on {% for k in keys %}w.{{ k }} = t.{{ k }}{% if not loop.last %} and {% endif %}{% endfor %}
),
ranked as (
    select
        *,
        pool_cents - sum(floor_cents) over (partition by {{ key_list }}) as leftover,
        row_number() over (partition by {{ key_list }} order by remainder desc, account_id asc) as place
    from shares
)
select
    {{ key_list }},
    account_id,
    cast(floor_cents + case when place <= leftover then 1 else 0 end as bigint) as allocated_cents
from ranked
{%- endmacro %}
