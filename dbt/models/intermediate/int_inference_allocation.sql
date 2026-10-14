-- Each provider invoice apportioned over the accounts that used the model, per pool, so the
-- accounts of a pool sum to its invoice to the cent.
{{ apportion(ref('int_inference_pools'), ref('int_inference_weights'), ['month', 'provider', 'model']) }}
