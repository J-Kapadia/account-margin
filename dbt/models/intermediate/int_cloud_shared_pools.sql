-- The untagged FOCUS pool of a month, once per allowed rule.
select p.month, r.rule, p.pool_cents
from (
    select month, cast(round(sum(effective_cost) * 100) as bigint) as pool_cents
    from {{ ref('stg_cloud_focus') }}
    where tenant is null
    group by month
) p
cross join (
    {% for rule in var('rules') -%}
    select '{{ rule }}' as rule{% if not loop.last %} union all {% endif %}
    {% endfor %}
) r
