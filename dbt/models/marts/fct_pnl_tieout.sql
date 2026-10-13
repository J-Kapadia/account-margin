-- The ledger against the account sums, per month and GL code: revenue lines by type, then the
-- three cost codes from the signed rule's account rows (cloud is direct plus allocated shared).
with accounts_side as (
    select
        month,
        case line_type
            when 'platform_fee' then '4000'
            when 'usage' then '4100'
            when 'credit' then '4900'
        end as gl_code,
        cast(sum(amount_cents) as bigint) as accounts_cents
    from {{ ref('stg_invoices') }}
    group by 1, 2
    union all
    select month, '5100', cast(sum(inference_cost_cents) as bigint) from {{ ref('fct_contribution_profit') }} group by month
    union all
    select month, '5200', cast(sum(cloud_direct_cents + cloud_shared_cents) as bigint)
    from {{ ref('fct_contribution_profit') }} group by month
    union all
    select month, '5300', cast(sum(support_cost_cents) as bigint) from {{ ref('fct_contribution_profit') }} group by month
),
gl_side as (
    select
        month,
        gl_code,
        cast(sum(amount_cents) as bigint) as gl_cents
    from {{ ref('stg_gl') }}
    where gl_code in ('4000', '4100', '4900', '5100', '5200', '5300')
    group by 1, 2
)
select
    coalesce(g.month, a.month) as month,
    coalesce(g.gl_code, a.gl_code) as gl_code,
    cast(coalesce(g.gl_cents, 0) as bigint) as gl_cents,
    cast(coalesce(a.accounts_cents, 0) as bigint) as accounts_cents,
    cast(coalesce(a.accounts_cents, 0) - coalesce(g.gl_cents, 0) as bigint) as diff_cents
from gl_side g
full outer join accounts_side a on g.month = a.month and g.gl_code = a.gl_code
order by month, gl_code
