-- The lower median of each reason-code driver over the classifier cohort of a month: accounts with
-- a usage row and positive revenue. The median is the value at rank ceil(n / 2), so it is always a
-- value that occurs in the data. Exact scaled integers, as in int_flag_drivers.
with cohort_values as (
    select
        month, code, value,
        row_number() over (partition by month, code order by value) as place,
        count(*) over (partition by month, code) as cohort_size
    from {{ ref('int_flag_drivers') }}
    where has_usage and revenue_cents > 0
)
select month, code, value as median, cast(cohort_size as integer) as cohort_size
from cohort_values
where place = (cohort_size + 1) // 2
