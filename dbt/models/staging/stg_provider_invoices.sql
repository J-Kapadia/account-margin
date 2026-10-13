select
    provider,
    model,
    cast(month || '-01' as date) as month,
    cast(tokens as bigint) as tokens,
    cast(amount_cents as bigint) as amount_cents,
    _row_hash
from {{ source('raw', 'provider_invoices') }}
