{{ config(alias='results') }}
-- The token-price scenario: contribution profit per input row (month, case, k, account). Python
-- writes the scaled inference cost to scenario.inputs; every other component is the base
-- month's, read from fct_contribution_profit. Profit, margin and the floor test go through the
-- same macros as the close, never a second copy of the formula.
with components as (
    select
        i.month,
        i."case",
        i.k,
        i.account_id,
        p.revenue_cents,
        cast(i.inference_cents as bigint) as inference_cost_cents,
        p.cloud_direct_cents,
        p.cloud_shared_cents,
        p.support_cost_cents
    from {{ source('scenario', 'inputs') }} i
    join {{ ref('fct_contribution_profit') }} p
        on p.month = i.month and p.account_id = i.account_id
),
profit as (
    select
        *,
        cast({{ contribution_cents('revenue_cents', 'inference_cost_cents', 'cloud_direct_cents',
                                   'cloud_shared_cents', 'support_cost_cents') }} as bigint)
            as contribution_profit_cents
    from components
),
flagged as (
    select
        *,
        cast({{ margin_pct_4dp('contribution_profit_cents', 'revenue_cents') }} as decimal(9, 4)) as margin_pct,
        {{ is_below('contribution_profit_cents', 'revenue_cents', var('floor_pct')) }} as below_floor,
        'Q' || cast((k - 1) // 3 + 1 as varchar) as quarter,
        (k % 3 = 0) as quarter_end
    from profit
)
select
    month,
    "case",
    k,
    account_id,
    revenue_cents,
    inference_cost_cents,
    cloud_direct_cents,
    cloud_shared_cents,
    support_cost_cents,
    contribution_profit_cents,
    margin_pct,
    below_floor,
    quarter,
    quarter_end,
    -- the first quarter whose last month is back above the floor; quarters sort as text
    coalesce(
        min(case when quarter_end and not below_floor then quarter end)
            over (partition by month, "case", account_id),
        'none'
    ) as recovery_quarter
from flagged
order by month, "case", account_id, k
