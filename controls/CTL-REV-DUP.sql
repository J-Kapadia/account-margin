-- CTL-REV-DUP: no invoice line is booked twice. Two checks over the invoice lines of :month::
--   * (invoice_id, line_id) is unique, so a line is not loaded twice under one invoice;
--   * the natural key (account_id, service_period, line_type, model, amount_cents) is unique, so
--     a line re-entered under a new line_id is caught. invoice_id alone is not the grain: an
--     invoice has several lines.
-- Returns one row per duplicated key, naming the revenue number the lines feed and every row.
with m as (select cast($month as date) as month),
lines as (
    select i.*, coalesce(i.model, '') as model_key
    from raw.invoices i, m
    where i._month = m.month
),
dups as (
    select 'invoice_id and line_id' as kind, account_id, invoice_id || ' / ' || line_id as key,
           list(_row_hash order by _row_hash) as hashes
    from lines
    group by account_id, invoice_id, line_id
    having count(*) > 1
    union all
    select 'natural key', account_id,
           account_id || ', ' || service_period || ', ' || line_type || ', ' || model_key || ', '
               || amount_cents,
           list(_row_hash order by _row_hash)
    from lines
    group by account_id, service_period, line_type, model_key, amount_cents
    having count(*) > 1
)
select
    'cp/' || strftime(m.month, '%Y-%m') || '/' || d.account_id || '/revenue' as number_id,
    d.hashes as row_hashes,
    'month ' || strftime(m.month, '%Y-%m') || ': duplicate invoice line by ' || d.kind || ' (' || d.key
        || '), ' || len(d.hashes) || ' rows' as description
from dups d, m
order by 1, 3
