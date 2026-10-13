{#- The one definition of contribution profit: revenue, net of credits and discounts,
    less inference, cloud (direct plus allocated shared) and support cost, in integer cents.
    Nothing else in dbt/ or src/ subtracts these components; a lint test enforces it. -#}
{% macro contribution_cents(revenue, inference, cloud_direct, cloud_shared, support) -%}
    ({{ revenue }} - {{ inference }} - {{ cloud_direct }} - {{ cloud_shared }} - {{ support }})
{%- endmacro %}
