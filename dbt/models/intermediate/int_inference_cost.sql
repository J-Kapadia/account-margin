-- Inference cost by account-month: the pools' allocations summed per account.
select month, account_id, cast(sum(allocated_cents) as bigint) as inference_cost_cents
from {{ ref('int_inference_allocation') }}
group by month, account_id
