-- CTL-PRICE-PIN: every model used has a sourced, dated price, and every FOCUS row is in USD,
-- starts and ends (UTC, `Z`) within the month, the end exclusive as in FOCUS, and carries at
-- most two decimals in every cost column the book has. A cost column the book has is filled on
-- every row, a ChargeCategory present is one of the five FOCUS values, and a covered row (one
-- with a CommitmentDiscountType) has a ListCost and a ContractedCost. Returns the violating rows
-- of :month.
--
-- A month with no provider invoice and no inference ledger entry has no inference side, so its
-- usage models need no price (the `small` fixture); in any other month there is no default price.
with m as (select cast($month as date) as month),
priced_month as (
    select m.month from m
    where exists (select 1 from raw.provider_invoices i where i._month = m.month)
       or exists (select 1 from raw.gl g where g._month = m.month and g.gl_code = '5100')
),
used as (
    select distinct u.model
    from raw.usage_monthly u join priced_month pm on u._month = pm.month
),
checked as (
    select
        u.model, p._row_hash as price_hash,
        try_cast(p.input_per_mtok as decimal(18, 6)) as price_in,
        try_cast(p.output_per_mtok as decimal(18, 6)) as price_out,
        case
            when p.model is null then 'no pinned price'
            when nullif(trim(p.source_url), '') is null then 'price has no source_url'
            when try_cast(nullif(trim(p.retrieved_on), '') as date) is null then 'price has no retrieved_on date'
            when try_cast(p.input_per_mtok as decimal(18, 6)) is null
              or try_cast(p.output_per_mtok as decimal(18, 6)) is null
              or try_cast(p.input_per_mtok as decimal(18, 6)) <= 0
              or try_cast(p.output_per_mtok as decimal(18, 6)) <= 0 then 'price is not a positive number'
            when try_cast(p.input_per_mtok as decimal(18, 6)) * 1000 <> floor(try_cast(p.input_per_mtok as decimal(18, 6)) * 1000)
              or try_cast(p.output_per_mtok as decimal(18, 6)) * 1000 <> floor(try_cast(p.output_per_mtok as decimal(18, 6)) * 1000)
              then 'price is not a whole number of nano-USD per token'
        end as problem
    from used u left join raw.ref_model_prices p on p.model = u.model
),
focus as (
    select
        c._row_hash, c._month, c."ResourceId" as resource_id,
        s.tag_class = 'direct' and not s.is_unused_commitment as direct,
        try_strptime(c."ChargePeriodStart", '%Y-%m-%dT%H:%M:%SZ') as period_start,
        try_strptime(c."ChargePeriodEnd", '%Y-%m-%dT%H:%M:%SZ') as period_end,
        c."BillingCurrency" as currency,
        c."ChargeCategory" as charge_category,
        nullif(trim(c."CommitmentDiscountType"), '') is not null as covered,
        -- Every cost column the book has is mandatory on every row. NULL is a column the file
        -- lacks (the two older columns are never lacking, so NULL there is an empty cell);
        -- '' is an empty cell of a column the file has.
        c."EffectiveCost" as effective_raw, c."BilledCost" as billed_raw,
        c."ListCost" as list_raw, c."ContractedCost" as contracted_raw
    from raw.cloud_focus c
    join m on c._month = m.month
    join stg.stg_cloud_focus s on s._row_hash = c._row_hash
),
costs as (
    -- one row per cost column present on a focus row
    select f._row_hash, v.name, nullif(trim(v.raw), '') as raw
    from focus f,
    lateral (
        select * from (values
            ('EffectiveCost', f.effective_raw, true), ('BilledCost', f.billed_raw, true),
            ('ListCost', f.list_raw, false), ('ContractedCost', f.contracted_raw, false)
        ) as t(name, raw, required)
        where t.required or t.raw is not null
    ) v
),
cost_problem as (
    select
        _row_hash,
        min(case when raw is null then name || ' is empty' end) as empty_cost,
        min(case when raw is not null
                  and (try_cast(raw as decimal(18, 6)) is null
                       or try_cast(raw as decimal(18, 6)) * 100 <> round(try_cast(raw as decimal(18, 6)) * 100))
                 then name || ' has more than two decimals or is not a number' end) as bad_cost
    from costs
    group by _row_hash
),
judged as (
    select
        f.*,
        case
            when f.currency is distinct from 'USD' then 'BillingCurrency is not USD'
            when f.period_start is null or f.period_end is null then 'charge period is not a UTC timestamp with a Z suffix'
            when f.period_start < cast(m.month as timestamp) then 'charge period starts before the month'
            when f.period_end > cast(m.month + interval 1 month as timestamp) then 'charge period ends after the first instant of the next month'
            when f.covered and (nullif(trim(f.list_raw), '') is null or nullif(trim(f.contracted_raw), '') is null)
                then 'covered row has no ListCost or ContractedCost, so it has no commitment benefit'
            when p.empty_cost is not null then p.empty_cost
            when f.charge_category is not null
             and f.charge_category not in ('Usage', 'Purchase', 'Tax', 'Credit', 'Adjustment')
                then 'ChargeCategory ' || case when f.charge_category = '' then 'is empty' else f.charge_category || ' is not one of Usage, Purchase, Tax, Credit, Adjustment' end
            when p.bad_cost is not null then p.bad_cost
        end as problem
    from focus f
    join m on true
    left join cost_problem p on p._row_hash = f._row_hash
)
select
    'rollup/' || strftime(m.month, '%Y-%m') || '/inference_cost' as number_id,
    list_concat(
        coalesce((select list(u._row_hash order by u._row_hash) from raw.usage_monthly u
                  where u._month = m.month and u.model = c.model), []),
        case when c.price_hash is null then [] else [c.price_hash] end
    ) as row_hashes,
    'model ' || c.model || ': ' || c.problem as description
from checked c, m
where c.problem is not null

union all
select
    'rollup/' || strftime(m.month, '%Y-%m') || case when f.direct then '/cloud_direct' else '/cloud_shared' end,
    [f._row_hash],
    'FOCUS row ' || f.resource_id || ': ' || f.problem
from judged f, m
where f.problem is not null
order by 1, 3
