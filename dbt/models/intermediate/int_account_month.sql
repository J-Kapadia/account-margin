-- Active accounts: any invoice, usage, tagged-cost or ticket row in the month.
select month, account_id from {{ ref('stg_invoices') }}
union
select month, account_id from {{ ref('stg_usage') }}
union
select month, tenant as account_id from {{ ref('stg_cloud_focus') }} where tenant is not null
union
select month, account_id from {{ ref('stg_support_tickets') }}
