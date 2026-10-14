-- CTL-TIE-INF: inference cost summed over accounts equals GL 5100, to the cent.
-- Returns the violating rows of :month (number_id, row_hashes, description, as every control).
select
    'pnl/' || strftime(t.month, '%Y-%m') || '/5100' as number_id,
    list_concat(
        coalesce((
            select list(g._row_hash order by g._row_hash) from raw.gl g
            where g.gl_code = '5100' and g._month = t.month
        ), []),
        coalesce((
            select list(p._row_hash order by p._row_hash) from raw.provider_invoices p
            where p._month = t.month
        ), [])
    ) as row_hashes,
    'GL 5100: ledger ' || t.gl_cents || ' cents, accounts ' || t.accounts_cents
        || ' cents, difference ' || t.diff_cents || ' cents' as description
from mart.fct_pnl_tieout t
where t.month = $month and t.gl_code = '5100' and t.diff_cents <> 0
order by number_id
