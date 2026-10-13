select
    account_id,
    cast(month || '-01' as date) as month,
    model,
    cast(input_tokens as bigint) as input_tokens,
    cast(output_tokens as bigint) as output_tokens,
    _row_hash
from {{ source('raw', 'usage_monthly') }}
