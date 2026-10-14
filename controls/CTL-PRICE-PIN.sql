-- CTL-PRICE-PIN: every model used has a sourced, dated price, and every FOCUS row is in USD,
-- starts and ends (UTC, `Z`) within the month, the end exclusive as in FOCUS, and carries at
-- most two decimals. Returns the violating rows of :month.
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
        nullif(json_extract_string(nullif(c."Tags", ''), '$.tenant'), '') is not null as tagged,
        try_strptime(c."ChargePeriodStart", '%Y-%m-%dT%H:%M:%SZ') as period_start,
        try_strptime(c."ChargePeriodEnd", '%Y-%m-%dT%H:%M:%SZ') as period_end,
        c."BillingCurrency" as currency,
        try_cast(c."EffectiveCost" as decimal(18, 6)) as effective_cost,
        try_cast(c."BilledCost" as decimal(18, 6)) as billed_cost
    from raw.cloud_focus c join m on c._month = m.month
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
    'rollup/' || strftime(m.month, '%Y-%m') || case when f.tagged then '/cloud_direct' else '/cloud_shared' end,
    [f._row_hash],
    'FOCUS row ' || f.resource_id || ': ' || case
        when f.currency is distinct from 'USD' then 'BillingCurrency is not USD'
        when f.period_start is null or f.period_end is null then 'charge period is not a UTC timestamp with a Z suffix'
        when f.period_start < cast(m.month as timestamp) then 'charge period starts before the month'
        when f.period_end > cast(m.month + interval 1 month as timestamp) then 'charge period ends after the first instant of the next month'
        else 'cost has more than two decimals or is not a number'
    end
from focus f, m
where f.currency is distinct from 'USD'
   or f.period_start is null or f.period_end is null
   or f.period_start < cast(m.month as timestamp)
   or f.period_end > cast(m.month + interval 1 month as timestamp)
   or f.effective_cost is null or f.billed_cost is null
   or f.effective_cost * 100 <> round(f.effective_cost * 100)
   or f.billed_cost * 100 <> round(f.billed_cost * 100)
order by 1, 3
