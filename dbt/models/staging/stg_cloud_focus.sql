-- One FOCUS row. The basis is EffectiveCost, an exact DECIMAL(18,6) that is never rounded per
-- row; a `tenant` tag makes the row direct cost of that account, no tag makes it shared.
-- The month is the loader's `_month`, the month of ChargePeriodStart.
select
    line_id,
    _month as month,
    cast("EffectiveCost" as decimal(18, 6)) as effective_cost,
    "ResourceId" as resource_id,
    nullif(json_extract_string(nullif("Tags", ''), '$.tenant'), '') as tenant,
    _row_hash
from {{ source('raw', 'cloud_focus') }}
