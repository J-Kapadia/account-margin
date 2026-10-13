-- Firm (below the floor under every allowed rule), soft (under some) or none, with a reason code
-- for every flagged account. The predicate is read from fct_contribution_by_rule.
{% set scale = 1000000000 %}
with per_rule as (
    select
        month,
        account_id,
        cast(sum(case when below_floor then 1 else 0 end) as integer) as rules_below_floor,
        cast(count(*) as integer) as rules_total
    from {{ ref('fct_contribution_by_rule') }}
    group by month, account_id
),
-- The classifier cohort fixes the medians only: a usage row and positive revenue.
cohort_values as (
    select
        month, code, value,
        row_number() over (partition by month, code order by value) as place,
        count(*) over (partition by month, code) as cohort_size
    from {{ ref('int_flag_drivers') }}
    where has_usage and revenue_cents > 0
),
medians as (
    -- the lower median: rank ceil(n / 2), always a value that occurs in the data
    select month, code, value as median from cohort_values where place = (cohort_size + 1) // 2
),
multiples as (
    select
        d.month, d.account_id, d.code, d.idx, d.revenue_cents,
        {{ half_even_div('d.value * ' ~ scale, 'greatest(coalesce(m.median, 0), d.median_floor)') }} as multiple
    from {{ ref('int_flag_drivers') }} d
    left join medians m on m.month = d.month and m.code = d.code
    where d.revenue_cents > 0
),
by_ratio as (
    select
        month, account_id,
        case when multiple > {{ scale }} then code else 'NONE' end as reason_code
    from (
        select *, row_number() over (partition by month, account_id order by multiple desc, idx) as place
        from multiples
    )
    where place = 1
),
by_cost as (
    -- revenue at or below zero skips the ratios: the largest of the three costs, ties in order
    select
        month, account_id,
        case
            when greatest(inference_cost_cents, support_cost_cents, cloud_direct_cents) = 0 then 'NONE'
            when inference_cost_cents = greatest(inference_cost_cents, support_cost_cents, cloud_direct_cents)
                then 'MODEL_MIX'
            when support_cost_cents = greatest(inference_cost_cents, support_cost_cents, cloud_direct_cents)
                then 'SUPPORT_LOAD'
            else 'DEDICATED_CLOUD'
        end as reason_code
    from {{ ref('fct_contribution_profit') }}
    where revenue_cents <= 0
),
reasons as (
    select * from by_ratio
    union all
    select * from by_cost
)
select
    p.month,
    p.account_id,
    case
        when p.rules_below_floor = p.rules_total and p.rules_total > 0 then 'firm'
        when p.rules_below_floor > 0 then 'soft'
        else 'none'
    end as flag,
    case when p.rules_below_floor > 0 then r.reason_code end as reason_code,
    p.rules_below_floor,
    p.rules_total
from per_rule p
left join reasons r on r.month = p.month and r.account_id = p.account_id
order by p.month, p.account_id
