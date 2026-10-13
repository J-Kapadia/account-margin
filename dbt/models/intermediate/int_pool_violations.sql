-- Pools the apportionment cannot cover, one row each, for CTL-ALLOC-CONSERVE. There is no
-- fallback to another rule: a pool with cents and no eligible weight stays unallocated.
-- Also: a usage model with no provider invoice or no pinned price, and a tenant tag that names
-- no known account.
select s.month, s.rule, 'shared cloud' as pool, s.pool_cents,
       'pool has cents and no account with weight under ' || s.rule as detail
from {{ ref('int_cloud_shared_pools') }} s
where s.pool_cents > 0
  and not exists (
      select 1 from {{ ref('int_cloud_shared_weights') }} w
      where w.month = s.month and w.rule = s.rule and w.weight > 0
  )
union all
select p.month, null, 'inference ' || p.provider || '/' || p.model, p.pool_cents,
       'provider invoice has cents and no account used the model'
from {{ ref('int_inference_pools') }} p
where p.pool_cents > 0
  and not exists (
      select 1 from {{ ref('int_inference_weights') }} w
      where w.month = p.month and w.provider = p.provider and w.model = p.model and w.weight > 0
  )
union all
select u.month, null, 'inference ' || u.model, cast(0 as bigint),
       'usage model has no provider invoice or no pinned price'
from (select distinct month, model from {{ ref('stg_usage') }}) u
where not exists (
    select 1
    from {{ ref('stg_model_prices') }} p
    join {{ ref('int_inference_pools') }} q on q.provider = p.provider and q.model = p.model
    where p.model = u.model and q.month = u.month
)
union all
select c.month, null, 'cloud direct ' || c.tenant, cast(0 as bigint),
       'tenant tag names an unknown account'
from (select distinct month, tenant from {{ ref('stg_cloud_focus') }} where tenant is not null) c
where c.tenant not in (select account_id from {{ ref('stg_accounts') }})
