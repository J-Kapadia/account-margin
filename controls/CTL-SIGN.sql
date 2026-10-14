-- CTL-SIGN: every amount has the sign of its kind. Returns, for :month:
--   * credit lines above zero, and platform-fee or usage charge lines below zero;
--   * cost GL entries (codes 5100 to 5300) below zero.
with m as (select cast($month as date) as month)
select
    'cp/' || strftime(m.month, '%Y-%m') || '/' || i.account_id || '/revenue' as number_id,
    [i._row_hash] as row_hashes,
    'month ' || strftime(m.month, '%Y-%m') || ': ' || i.line_type || ' line ' || i.line_id || ' is '
        || i.amount_cents || ' cents, ' || case i.line_type when 'credit' then 'a credit must be <= 0'
                                                            else 'a charge must be >= 0' end as description
from raw.invoices i, m
where i._month = m.month
  and ((i.line_type = 'credit' and cast(i.amount_cents as bigint) > 0)
    or (i.line_type <> 'credit' and cast(i.amount_cents as bigint) < 0))

union all
select
    'pnl/' || strftime(m.month, '%Y-%m') || '/' || g.gl_code,
    [g._row_hash],
    'month ' || strftime(m.month, '%Y-%m') || ': cost entry ' || g.entry_id || ' in GL ' || g.gl_code
        || ' is ' || g.amount_cents || ' cents, a cost must be >= 0'
from raw.gl g, m
where g._month = m.month and g.gl_code like '5%' and cast(g.amount_cents as bigint) < 0
order by 1, 3
