-- A ticket costs in the month of its closed_date; one still open has no month and no cost.
select
    ticket_id,
    account_id,
    _month as month,
    cast(handle_minutes as bigint) as handle_minutes,
    tier,
    _row_hash
from {{ source('raw', 'support_tickets') }}
where _month is not null
