select
    tier,
    cast(cents_per_minute as bigint) as cents_per_minute,
    _row_hash
from {{ source('raw', 'ref_support_rates') }}
