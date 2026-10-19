-- FOCUS rows with a tenant tag are the direct cloud cost of that account; a commitment that
-- went unused is a pool row whatever its tag.
select
    month,
    tenant as account_id,
    sum(effective_cents) as cloud_direct_cents
from {{ ref('stg_cloud_focus') }}
where tag_class = 'direct' and not is_unused_commitment
group by month, tenant
