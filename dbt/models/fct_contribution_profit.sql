-- Phase 0 carries revenue only. The cost components, the contribution_cents() macro and
-- the allocation rules arrive in Phase 3; nothing here subtracts anything.
select
    month,
    account_id,
    cast(sum(amount_cents) as bigint) as revenue_cents
from {{ ref('stg_invoices') }}
group by month, account_id
order by month, account_id
