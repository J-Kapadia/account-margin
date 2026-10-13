-- What the shared-cloud weights read: revenue, raw tokens and whether the account had a usage row.
select
    am.month,
    am.account_id,
    cast(coalesce(r.revenue_cents, 0) as bigint) as revenue_cents,
    cast(coalesce(u.total_tokens, 0) as hugeint) as total_tokens,
    u.account_id is not null as has_usage
from {{ ref('int_account_month') }} am
left join {{ ref('int_revenue') }} r on r.month = am.month and r.account_id = am.account_id
left join (
    select month, account_id, sum(input_tokens + output_tokens) as total_tokens
    from {{ ref('stg_usage') }}
    group by month, account_id
) u on u.month = am.month and u.account_id = am.account_id
