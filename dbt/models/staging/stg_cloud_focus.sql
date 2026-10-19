-- One FOCUS row. Cents are formed once, here, from each cost column as `round(cost * 100)`;
-- every model downstream sums these cents and rounds nothing. A cost cell that is empty or not a
-- number is NULL cents (CTL-PRICE-PIN names the row), and `ListCost` and `ContractedCost` fall
-- back to `EffectiveCost` when their column is absent from the book or empty on the row.
-- The six optional columns are NULL in `raw` when the file lacks them and '' when it has them
-- and the cell is empty. The month is the loader's `_month`, the month of ChargePeriodStart.
-- `tenant` is the tag of an account; the reserved value `shared` is not an account, so it is
-- NULL here and `tag_class` says which of the three kinds the row is.
with typed as (
    select
        line_id,
        _month as month,
        try_cast(nullif(trim("EffectiveCost"), '') as decimal(18, 6)) as effective_cost,
        try_cast(nullif(trim("BilledCost"), '') as decimal(18, 6)) as billed_cost,
        try_cast(nullif(trim("ListCost"), '') as decimal(18, 6)) as list_cost,
        try_cast(nullif(trim("ContractedCost"), '') as decimal(18, 6)) as contracted_cost,
        coalesce("ChargeCategory", 'Usage') as charge_category,
        nullif("CommitmentDiscountCategory", '') as commitment_discount_category,
        "ResourceId" as resource_id,
        "ServiceName" as service_name,
        nullif("RegionId", '') as region_id,
        nullif(json_extract_string(nullif("Tags", ''), '$.tenant'), '') as tenant_tag,
        nullif(json_extract_string(nullif("Tags", ''), '$.product'), '') as product,
        nullif(json_extract_string(nullif("Tags", ''), '$.team'), '') as team,
        _row_hash
    from {{ source('raw', 'cloud_focus') }}
),
cents as (
    select
        *,
        cast(round(effective_cost * 100) as bigint) as effective_cents,
        cast(round(billed_cost * 100) as bigint) as billed_cents,
        cast(round(coalesce(list_cost, effective_cost) * 100) as bigint) as list_cents,
        cast(round(coalesce(contracted_cost, effective_cost) * 100) as bigint) as contracted_cents
    from typed
)
select
    line_id,
    month,
    list_cents,
    contracted_cents,
    effective_cents,
    billed_cents,
    charge_category,
    charge_category = 'Usage' and commitment_discount_category is not null as is_covered,
    case
        when charge_category = 'Usage' and commitment_discount_category is not null
            then coalesce(contracted_cents - effective_cents, 0)
        else 0
    end as benefit_cents,
    coalesce(
        charge_category = 'Purchase' and commitment_discount_category is not null and effective_cents <> 0,
        false
    ) as is_unused_commitment,
    case
        when tenant_tag = 'shared' then 'shared'
        when tenant_tag is not null then 'direct'
        else 'untagged'
    end as tag_class,
    case when tenant_tag = 'shared' then null else tenant_tag end as tenant,
    product,
    team,
    region_id,
    service_name,
    resource_id,
    _row_hash
from cents
