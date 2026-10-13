-- Handled minutes times the per-minute rate of the ticket's tier, in the month the ticket closed.
select
    t.month,
    t.account_id,
    cast(sum(t.handle_minutes * r.cents_per_minute) as bigint) as support_cost_cents
from {{ ref('stg_support_tickets') }} t
join {{ ref('stg_support_rates') }} r on r.tier = t.tier
group by t.month, t.account_id
