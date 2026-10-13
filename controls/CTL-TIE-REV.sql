-- CTL-TIE-REV: account revenue equals the general ledger, to the cent, per revenue GL code.
-- Returns the violating rows of :month. Convention for every control: number_id, row_hashes
-- (the raw rows behind the number) and a description.
select
    'pnl/' || strftime(t.month, '%Y-%m') || '/' || t.gl_code as number_id,
    list_concat(
        coalesce((
            select list(g._row_hash order by g._row_hash) from raw.gl g
            where g.gl_code = t.gl_code and g._month = t.month
        ), []),
        coalesce((
            select list(i._row_hash order by i._row_hash) from raw.invoices i
            where i._month = t.month
              and i.line_type = case t.gl_code when '4000' then 'platform_fee'
                                               when '4100' then 'usage' else 'credit' end
        ), [])
    ) as row_hashes,
    'GL ' || t.gl_code || ': ledger ' || t.gl_cents || ' cents, accounts ' || t.accounts_cents
        || ' cents, difference ' || t.diff_cents || ' cents' as description
from mart.fct_pnl_tieout t
where t.month = $month and t.gl_code in ('4000', '4100', '4900') and t.diff_cents <> 0
order by number_id
