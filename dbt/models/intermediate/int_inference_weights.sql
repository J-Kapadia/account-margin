-- An account's weight in a pool: its tokens for the model times the pinned price, per direction.
select
    u.month,
    p.provider,
    u.model,
    u.account_id,
    {{ inference_weight('u.input_tokens', 'u.output_tokens', 'p.nano_in', 'p.nano_out') }} as weight
from (
    select month, model, account_id, sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens
    from {{ ref('stg_usage') }}
    group by month, model, account_id
) u
join {{ ref('stg_model_prices') }} p on p.model = u.model
