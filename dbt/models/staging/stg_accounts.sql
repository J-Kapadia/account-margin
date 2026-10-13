select
    account_id,
    display_name,
    plan,
    cast(start_date as date) as start_date,
    cast(discount_pct as decimal(18, 9)) as discount_pct,
    _row_hash
from {{ source('raw', 'accounts') }}
