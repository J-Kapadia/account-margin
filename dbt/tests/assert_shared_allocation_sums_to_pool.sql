-- Conservation: under every rule, a shared pool that has any eligible weight is allocated in
-- full. A pool with no eligible weight is not a conservation failure here; it is reported by
-- int_pool_violations.
select p.month, p.rule, p.pool_cents, coalesce(a.allocated, 0) as allocated_cents
from {{ ref('int_cloud_shared_pools') }} p
left join (
    select month, rule, sum(allocated_cents) as allocated from {{ ref('int_cloud_shared') }} group by month, rule
) a on a.month = p.month and a.rule = p.rule
where exists (
    select 1 from {{ ref('int_cloud_shared_weights') }} w
    where w.month = p.month and w.rule = p.rule and w.weight > 0
)
  and coalesce(a.allocated, 0) <> p.pool_cents
