-- Typing only. Rows are carried as booked (signed); the row hash is carried, not recomputed.
select
    invoice_id,
    line_id,
    account_id,
    cast(service_period || '-01' as date) as month,
    cast(gl_period || '-01' as date) as gl_month,
    cast(issued_date as date) as issued_date,
    line_type,
    nullif(model, '') as model,
    cast(nullif(quantity_tokens, '') as bigint) as quantity_tokens,
    cast(nullif(unit_price, '') as decimal(18, 4)) as unit_price,
    cast(amount_cents as bigint) as amount_cents,
    _row_hash
from {{ source('raw', 'invoices') }}
