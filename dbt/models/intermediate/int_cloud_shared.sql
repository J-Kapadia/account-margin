-- Shared cloud cost allocated to accounts under each allowed rule.
{{ apportion(ref('int_cloud_shared_pools'), ref('int_cloud_shared_weights'), ['month', 'rule']) }}
