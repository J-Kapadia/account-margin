-- One pool per (month, provider, model): the provider's invoice, in cents.
select month, provider, model, cast(sum(amount_cents) as bigint) as pool_cents
from {{ ref('stg_provider_invoices') }}
group by month, provider, model
