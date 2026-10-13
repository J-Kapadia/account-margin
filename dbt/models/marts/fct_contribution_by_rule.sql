-- Contribution profit per account-month under every allowed allocation rule. The only model that
-- calls contribution_cents(); fct_contribution_profit is this table filtered to the signed rule.
with rules as (
    {% for rule in var('rules') -%}
    select '{{ rule }}' as rule{% if not loop.last %} union all {% endif %}
    {% endfor %}
),
components as (
    select
        f.month,
        r.rule,
        f.account_id,
        f.revenue_cents,
        cast(coalesce(i.inference_cost_cents, 0) as bigint) as inference_cost_cents,
        cast(coalesce(d.cloud_direct_cents, 0) as bigint) as cloud_direct_cents,
        cast(coalesce(s.allocated_cents, 0) as bigint) as cloud_shared_cents,
        cast(coalesce(p.support_cost_cents, 0) as bigint) as support_cost_cents
    from {{ ref('int_account_facts') }} f
    cross join rules r
    left join {{ ref('int_inference_cost') }} i on i.month = f.month and i.account_id = f.account_id
    left join {{ ref('int_cloud_direct') }} d on d.month = f.month and d.account_id = f.account_id
    left join {{ ref('int_cloud_shared') }} s
        on s.month = f.month and s.rule = r.rule and s.account_id = f.account_id
    left join {{ ref('int_support_cost') }} p on p.month = f.month and p.account_id = f.account_id
),
profit as (
    select
        *,
        cast({{ contribution_cents('revenue_cents', 'inference_cost_cents', 'cloud_direct_cents',
                                   'cloud_shared_cents', 'support_cost_cents') }} as bigint)
            as contribution_profit_cents,
        year(month) * 12 + month(month) as month_index
    from components
),
windowed as (
    select
        *,
        sum(contribution_profit_cents) over w as trailing3_profit_cents,
        sum(revenue_cents) over w as trailing3_revenue_cents
    from profit
    window w as (
        partition by rule, account_id order by month_index range between 2 preceding and current row
    )
)
select
    month,
    rule,
    account_id,
    revenue_cents,
    inference_cost_cents,
    cloud_direct_cents,
    cloud_shared_cents,
    support_cost_cents,
    contribution_profit_cents,
    cast({{ margin_pct_4dp('contribution_profit_cents', 'revenue_cents') }} as decimal(9, 4)) as margin_pct,
    cast({{ margin_pct_4dp('trailing3_profit_cents', 'trailing3_revenue_cents') }} as decimal(9, 4))
        as trailing3_margin_pct,
    {{ is_below('contribution_profit_cents', 'revenue_cents', var('floor_pct')) }} as below_floor
from windowed
order by month, rule, account_id
