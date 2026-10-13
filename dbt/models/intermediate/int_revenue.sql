-- Revenue by account-month, net of credits and discounts (usage lines are booked net).
-- The line-type split feeds the list-price revenue of the reason-code classifier.
select
    month,
    account_id,
    cast(sum(amount_cents) as bigint) as revenue_cents,
    cast(sum(case when line_type = 'platform_fee' then amount_cents else 0 end) as bigint) as fee_cents,
    cast(sum(case when line_type = 'usage' then amount_cents else 0 end) as bigint) as usage_amount_cents
from {{ ref('stg_invoices') }}
group by month, account_id
