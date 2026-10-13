{#- The apportionment weight of an account for each pool. Weights are integers.
    Shared cloud (relation int_account_facts: month, account_id, revenue_cents, total_tokens,
    has_usage):
      by_tokens    raw input plus output tokens, no price
      by_revenue   max(revenue_cents, 0)
      equal_usage  1 for each account with at least one usage row in the month -#}
{% macro shared_weight(rule) -%}
{%- if rule == 'by_tokens' -%}
cast(total_tokens as hugeint)
{%- elif rule == 'by_revenue' -%}
cast(greatest(revenue_cents, 0) as hugeint)
{%- elif rule == 'equal_usage' -%}
cast(case when has_usage then 1 else 0 end as hugeint)
{%- else -%}
{{ exceptions.raise_compiler_error("unknown allocation rule " ~ rule) }}
{%- endif -%}
{%- endmacro %}

{#- Inference (provider, model, month): tokens times the pinned price, as exact integers.
    Prices are integer nano-USD per token, so every weight is an exact integer. -#}
{% macro inference_weight(input_tokens, output_tokens, nano_in, nano_out) -%}
(cast({{ input_tokens }} as hugeint) * {{ nano_in }} + cast({{ output_tokens }} as hugeint) * {{ nano_out }})
{%- endmacro %}
