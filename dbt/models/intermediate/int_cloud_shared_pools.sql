-- The FOCUS pool of a month, once per allowed rule: untagged rows, rows tagged with the reserved
-- value `shared`, and unused commitment rows whatever their tag.
select p.month, r.rule, p.pool_cents
from (
    select month, sum(effective_cents) as pool_cents
    from {{ ref('stg_cloud_focus') }}
    where tag_class <> 'direct' or is_unused_commitment
    group by month
) p
cross join (
    {% for rule in var('rules') -%}
    select '{{ rule }}' as rule{% if not loop.last %} union all {% endif %}
    {% endfor %}
) r
