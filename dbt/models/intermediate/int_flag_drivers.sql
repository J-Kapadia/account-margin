-- The four reason-code drivers per account-month, exact and in integers.
-- Every ratio is scaled by 1e9 and rounded half-even, matching DECIMAL(18,9) in the Python
-- reference, so no float enters a flag decision. A ratio is cost over list-price revenue:
-- platform fee plus usage restored to list price (usage / (1 - discount_pct)), before credits.
-- With discount D scaled by 1e9:  cost / list = cost * (1e9 - D) / (fee * (1e9 - D) + usage * 1e9).
{% set scale = 1000000000 %}
with base as (
    select
        p.month,
        p.account_id,
        p.revenue_cents,
        p.inference_cost_cents,
        p.support_cost_cents,
        p.cloud_direct_cents,
        f.has_usage,
        cast(cast(a.discount_pct * {{ scale }} as bigint) as hugeint) as discount,
        cast(coalesce(r.fee_cents, 0) as hugeint) as fee_cents,
        cast(coalesce(r.usage_amount_cents, 0) as hugeint) as usage_amount_cents
    from {{ ref('fct_contribution_profit') }} p
    join {{ ref('int_account_facts') }} f on f.month = p.month and f.account_id = p.account_id
    left join {{ ref('stg_accounts') }} a on a.account_id = p.account_id
    left join {{ ref('int_revenue') }} r on r.month = p.month and r.account_id = p.account_id
),
priced as (
    select
        *,
        fee_cents * ({{ scale }} - discount) + usage_amount_cents * {{ scale }} as list_denominator
    from base
)
{% for d in var('drivers') %}
select
    month, account_id, revenue_cents, has_usage, '{{ d.code }}' as code,
    {{ loop.index }} as idx,
    cast(cast('{{ d.median_floor }}' as decimal(18, 9)) * {{ scale }} as bigint) as median_floor,
    {% if d.code == 'DEEP_DISCOUNT' -%}
    discount
    {%- else -%}
    {{ half_even_div(
        (
            'cast(' ~ {'MODEL_MIX': 'inference_cost_cents', 'SUPPORT_LOAD': 'support_cost_cents',
                       'DEDICATED_CLOUD': 'cloud_direct_cents'}[d.code] ~ ' as hugeint) * ('
            ~ scale ~ ' - discount) * ' ~ scale
        ), 'list_denominator') }}
    {%- endif %} as value
from priced
{% if not loop.last %}union all{% endif %}
{% endfor %}
