-- Revenue GL codes against the account sums of the invoice lines that book to them.
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
),
gl_side as (
    select
        cast(gl_period || '-01' as date) as month,
        gl_code,
        cast(sum(cast(amount_cents as bigint)) as bigint) as gl_cents
    from {{ source('raw', 'gl') }}
    where gl_code in ('4000', '4100', '4900')
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
