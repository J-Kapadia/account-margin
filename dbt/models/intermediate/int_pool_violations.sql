-- Pools the apportionment cannot cover, one row each, for CTL-ALLOC-CONSERVE. There is no
-- fallback to another rule: a pool with cents and no eligible weight stays unallocated.
-- Also: a usage model with no provider invoice, and a tenant tag that names no known account.
-- `kind` and the key columns let the control find the source rows of each violation.
select s.month, s.rule, 'shared cloud' as pool, s.pool_cents,
       'pool has cents and no account with weight under ' || s.rule as detail,
       'shared_pool' as kind, cast(null as varchar) as provider, cast(null as varchar) as model,
       cast(null as varchar) as tenant
from {{ ref('int_cloud_shared_pools') }} s
where s.pool_cents > 0
  and not exists (
      select 1 from {{ ref('int_cloud_shared_weights') }} w
      where w.month = s.month and w.rule = s.rule and w.weight > 0
  )
union all
select p.month, null, 'inference ' || p.provider || '/' || p.model, p.pool_cents,
       'provider invoice has cents and no account used the model',
       'inference_pool', p.provider, p.model, null
from {{ ref('int_inference_pools') }} p
where p.pool_cents > 0
  and not exists (
      select 1 from {{ ref('int_inference_weights') }} w
      where w.month = p.month and w.provider = p.provider and w.model = p.model and w.weight > 0
  )
union all
-- A month with no provider invoice and no inference ledger entry has no inference side to cover
-- (the `small` fixture); in any other month every model in usage needs its provider invoice.
select u.month, null, 'inference ' || u.model, cast(0 as bigint),
       'usage model has no provider invoice',
       'usage_model', null, u.model, null
from (select distinct month, model from {{ ref('stg_usage') }}) u
where (
        exists (select 1 from {{ ref('stg_provider_invoices') }} i where i.month = u.month)
        or exists (select 1 from {{ ref('stg_gl') }} g where g.month = u.month and g.gl_code = '5100')
      )
  and not exists (
      select 1 from {{ ref('stg_provider_invoices') }} i where i.month = u.month and i.model = u.model
  )
union all
select c.month, null, 'cloud direct ' || c.tenant, cast(0 as bigint),
       'tenant tag names an unknown account',
       'unknown_tenant', null, null, c.tenant
from (select distinct month, tenant from {{ ref('stg_cloud_focus') }} where tenant is not null) c
where c.tenant not in (select account_id from {{ ref('stg_accounts') }})
