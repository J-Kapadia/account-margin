-- FOCUS rows with a tenant tag are the direct cloud cost of that account.
select
    month,
    tenant as account_id,
    cast(round(sum(effective_cost) * 100) as bigint) as cloud_direct_cents
from {{ ref('stg_cloud_focus') }}
where tenant is not null
group by month, tenant
