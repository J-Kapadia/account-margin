-- The signed rule's rows of fct_contribution_by_rule. No arithmetic of its own: every
-- interface reads contribution profit from this table.
select
    month,
    account_id,
    revenue_cents,
    inference_cost_cents,
    cloud_direct_cents,
    cloud_shared_cents,
    support_cost_cents,
    contribution_profit_cents,
    margin_pct,
    trailing3_margin_pct
from {{ ref('fct_contribution_by_rule') }}
where rule = '{{ var("signed_rule") }}'
order by month, account_id
