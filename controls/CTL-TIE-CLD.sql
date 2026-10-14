-- CTL-TIE-CLD: cloud cost (direct plus allocated shared) summed over accounts equals GL 5200,
-- to the cent. Returns the violating rows of :month.
select
    'pnl/' || strftime(t.month, '%Y-%m') || '/5200' as number_id,
    list_concat(
        coalesce((
            select list(g._row_hash order by g._row_hash) from raw.gl g
            where g.gl_code = '5200' and g._month = t.month
        ), []),
        coalesce((
            select list(c._row_hash order by c._row_hash) from raw.cloud_focus c
            where c._month = t.month
        ), [])
    ) as row_hashes,
    'GL 5200: ledger ' || t.gl_cents || ' cents, accounts ' || t.accounts_cents
        || ' cents, difference ' || t.diff_cents || ' cents' as description
from mart.fct_pnl_tieout t
where t.month = $month and t.gl_code = '5200' and t.diff_cents <> 0
order by number_id
