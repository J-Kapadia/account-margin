-- Prices are integer nano-USD per token (USD per million tokens x 1000), so weights are exact.
select
    provider,
    model,
    cast(cast(input_per_mtok as decimal(18, 6)) * 1000 as bigint) as nano_in,
    cast(cast(output_per_mtok as decimal(18, 6)) * 1000 as bigint) as nano_out,
    _row_hash
from {{ source('raw', 'ref_model_prices') }}
