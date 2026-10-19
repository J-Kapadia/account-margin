-- CTL-ALLOC-CONSERVE: every pool is allocated in full and nothing is counted twice. Returns the
-- violating rows of :month:
--   * a pool with cents and no account with weight (shared cloud under a rule, or a provider
--     invoice no account used), a usage model with no provider invoice, and a tenant tag that
--     names no known account, the reserved value `shared` being none (int_pool_violations; no
--     fallback to another rule);
--   * a pool with an eligible weight whose allocation does not sum to its cents;
--   * a FOCUS charge (same resource and charge period) that is both tagged and untagged, so it
--     would be counted as direct and as shared cost.
with m as (select cast($month as date) as month),
-- The pool rows are those with no tenant tag, the reserved tag `shared`, and unused commitments.
untagged as (
    select c.*
    from raw.cloud_focus c
    join m on c._month = m.month
    join stg.stg_cloud_focus s on s._row_hash = c._row_hash
    where s.tag_class <> 'direct' or s.is_unused_commitment
),
tagged as (
    select c.*, s.tenant
    from raw.cloud_focus c
    join m on c._month = m.month
    join stg.stg_cloud_focus s on s._row_hash = c._row_hash
    where s.tag_class = 'direct' and not s.is_unused_commitment
)
select
    case v.kind
        when 'shared_pool' then 'rollup/' || strftime(v.month, '%Y-%m') || '/cloud_shared'
        when 'unknown_tenant' then 'rollup/' || strftime(v.month, '%Y-%m') || '/cloud_direct'
        else 'rollup/' || strftime(v.month, '%Y-%m') || '/inference_cost'
    end as number_id,
    case v.kind
        when 'shared_pool' then coalesce((select list(u._row_hash order by u._row_hash) from untagged u), [])
        when 'inference_pool' then coalesce((
            select list(p._row_hash order by p._row_hash) from raw.provider_invoices p
            where p._month = v.month and p.provider = v.provider and p.model = v.model), [])
        when 'usage_model' then coalesce((
            select list(u._row_hash order by u._row_hash) from raw.usage_monthly u
            where u._month = v.month and u.model = v.model), [])
        else coalesce((select list(t._row_hash order by t._row_hash) from tagged t where t.tenant = v.tenant), [])
    end as row_hashes,
    'month ' || strftime(v.month, '%Y-%m') || ', rule ' || coalesce(v.rule, 'n/a') || ', pool ' || v.pool
        || ' (' || v.pool_cents || ' cents): ' || v.detail as description
from intermediate.int_pool_violations v, m
where v.month = m.month

union all
select
    'rollup/' || strftime(p.month, '%Y-%m') || '/cloud_shared',
    coalesce((select list(u._row_hash order by u._row_hash) from untagged u), []),
    'month ' || strftime(p.month, '%Y-%m') || ', rule ' || p.rule || ', pool shared cloud (' || p.pool_cents
        || ' cents): allocated ' || coalesce(a.allocated, 0) || ' cents'
from intermediate.int_cloud_shared_pools p
join m on p.month = m.month
left join (
    select month, rule, sum(allocated_cents) as allocated from intermediate.int_cloud_shared group by month, rule
) a on a.month = p.month and a.rule = p.rule
where exists (select 1 from intermediate.int_cloud_shared_weights w
              where w.month = p.month and w.rule = p.rule and w.weight > 0)
  and coalesce(a.allocated, 0) <> p.pool_cents

union all
select
    'rollup/' || strftime(p.month, '%Y-%m') || '/inference_cost',
    coalesce((select list(i._row_hash order by i._row_hash) from raw.provider_invoices i
              where i._month = p.month and i.provider = p.provider and i.model = p.model), []),
    'month ' || strftime(p.month, '%Y-%m') || ', rule n/a, pool inference ' || p.provider || '/' || p.model
        || ' (' || p.pool_cents || ' cents): allocated ' || coalesce(a.allocated, 0) || ' cents'
from intermediate.int_inference_pools p
join m on p.month = m.month
left join (
    select month, provider, model, sum(allocated_cents) as allocated
    from intermediate.int_inference_allocation group by month, provider, model
) a on a.month = p.month and a.provider = p.provider and a.model = p.model
where exists (select 1 from intermediate.int_inference_weights w
              where w.month = p.month and w.provider = p.provider and w.model = p.model and w.weight > 0)
  and coalesce(a.allocated, 0) <> p.pool_cents

union all
select
    'cp/' || strftime(t._month, '%Y-%m') || '/' || t.tenant || '/cloud_direct',
    list_sort(list_concat([t._row_hash], [u._row_hash])),
    'month ' || strftime(t._month, '%Y-%m') || ', rule n/a, pool cloud ' || t."ResourceId"
        || ': the charge is tagged to ' || t.tenant || ' and also untagged, so it is direct and shared'
from tagged t
join untagged u
  on u."ResourceId" = t."ResourceId" and u."ChargePeriodStart" = t."ChargePeriodStart"
 and u."ChargePeriodEnd" = t."ChargePeriodEnd"
order by 1, 3
