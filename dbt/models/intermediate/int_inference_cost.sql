-- Inference cost by account-month: each provider invoice apportioned over the accounts that used
-- the model, so the accounts sum to the invoice to the cent.
select month, account_id, cast(sum(allocated_cents) as bigint) as inference_cost_cents
from (
    {{ apportion(ref('int_inference_pools'), ref('int_inference_weights'), ['month', 'provider', 'model']) }}
)
group by month, account_id
