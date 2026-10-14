-- CTL-TIE-SUP: support cost, the handled minutes of the tickets closed in the month times their
-- tier rate, equals GL 5300 to the cent. The two figures are produced independently, so the
-- control can fail. A ledger total with no ticket cost, or ticket cost with no ledger total,
-- is a failure and is named as such, never a silent drop. Returns the violating rows of :month.
select
    'pnl/' || strftime(t.month, '%Y-%m') || '/5300' as number_id,
    list_concat(
        coalesce((
            select list(g._row_hash order by g._row_hash) from raw.gl g
            where g.gl_code = '5300' and g._month = t.month
        ), []),
        coalesce((
            select list(k._row_hash order by k._row_hash) from raw.support_tickets k
            where k._month = t.month
        ), [])
    ) as row_hashes,
    'GL 5300: ledger ' || t.gl_cents || ' cents, accounts ' || t.accounts_cents
        || ' cents, difference ' || t.diff_cents || ' cents'
        || case
            when t.accounts_cents = 0 then ' (the ledger carries support cost and no ticket closed in the month does)'
            when t.gl_cents = 0 then ' (tickets closed in the month carry cost and the ledger has none)'
            else ''
        end as description
from mart.fct_pnl_tieout t
where t.month = $month and t.gl_code = '5300' and t.diff_cents <> 0
order by number_id
