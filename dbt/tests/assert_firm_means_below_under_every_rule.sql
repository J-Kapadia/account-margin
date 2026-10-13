-- A firm flag implies below the floor under all allowed rules; a soft flag under some but not all.
select f.month, f.account_id, f.flag, f.rules_below_floor, f.rules_total
from {{ ref('fct_flags') }} f
where (f.flag = 'firm' and f.rules_below_floor <> f.rules_total)
   or (f.flag = 'soft' and (f.rules_below_floor = 0 or f.rules_below_floor = f.rules_total))
   or (f.flag = 'none' and f.rules_below_floor <> 0)
