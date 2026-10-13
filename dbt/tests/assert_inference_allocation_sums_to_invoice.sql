-- Conservation: every provider invoice with an eligible account is allocated to the cent.
select p.month, p.provider, p.model, p.pool_cents, coalesce(a.allocated, 0) as allocated_cents
from {{ ref('int_inference_pools') }} p
left join (
    select month, provider, model, sum(allocated_cents) as allocated
    from (
        {{ apportion(ref('int_inference_pools'), ref('int_inference_weights'), ['month', 'provider', 'model']) }}
    )
    group by month, provider, model
) a on a.month = p.month and a.provider = p.provider and a.model = p.model
where exists (
    select 1 from {{ ref('int_inference_weights') }} w
    where w.month = p.month and w.provider = p.provider and w.model = p.model and w.weight > 0
)
  and coalesce(a.allocated, 0) <> p.pool_cents
