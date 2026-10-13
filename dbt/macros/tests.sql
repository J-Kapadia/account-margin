{#- Generic tests. dbt packages are not installed (no `dbt deps`), so the two the models need are here. -#}
{% test unique_combination(model, columns) %}
select {{ columns | join(', ') }}, count(*) as rows_found
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}

{% test value_in_range(model, column_name, min_value, max_value_exclusive) %}
select {{ column_name }}
from {{ model }}
where {{ column_name }} < {{ min_value }} or {{ column_name }} >= {{ max_value_exclusive }}
{% endtest %}
