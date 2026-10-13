-- The weight of every active account under every allowed rule.
{% for rule in var('rules') -%}
select month, '{{ rule }}' as rule, account_id, {{ shared_weight(rule) }} as weight
from {{ ref('int_account_facts') }}
{% if not loop.last %}union all{% endif %}
{% endfor %}
