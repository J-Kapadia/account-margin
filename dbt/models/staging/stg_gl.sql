select
    entry_id,
    cast(gl_period || '-01' as date) as month,
    gl_code,
    cast(amount_cents as bigint) as amount_cents,
    _row_hash
from {{ source('raw', 'gl') }}
