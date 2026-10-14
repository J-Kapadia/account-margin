-- CTL-CUTOFF: revenue is booked in the period it was earned. Returns, for :month:
--   * invoice lines whose gl_period is not their service_period (the load month): the line is in
--     this month's revenue but the ledger books it in another period;
--   * usage lines with no metered usage for the same account and model in the service period.
with m as (select cast($month as date) as month)
select
    'cp/' || strftime(m.month, '%Y-%m') || '/' || i.account_id || '/revenue' as number_id,
    [i._row_hash] as row_hashes,
    'month ' || strftime(m.month, '%Y-%m') || ': invoice line ' || i.line_id || ' has service period '
        || i.service_period || ' but gl period ' || i.gl_period as description
from raw.invoices i, m
where i._month = m.month and i.gl_period <> i.service_period

union all
select
    'cp/' || strftime(m.month, '%Y-%m') || '/' || i.account_id || '/revenue',
    [i._row_hash],
    'month ' || strftime(m.month, '%Y-%m') || ': usage line ' || i.line_id || ' bills ' || i.model
        || ' with no usage in ' || i.service_period
from raw.invoices i, m
where i._month = m.month and i.line_type = 'usage'
  and not exists (
      select 1 from raw.usage_monthly u
      where u._month = m.month and u.account_id = i.account_id and u.model = i.model)
order by 1, 3
