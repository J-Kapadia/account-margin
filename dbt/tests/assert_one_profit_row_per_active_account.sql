-- fct_contribution_profit holds exactly the signed rule's rows of fct_contribution_by_rule.
select b.month, b.account_id
from {{ ref('fct_contribution_by_rule') }} b
left join {{ ref('fct_contribution_profit') }} p
    on p.month = b.month and p.account_id = b.account_id
where b.rule = '{{ var("signed_rule") }}'
  and (p.account_id is null or p.contribution_profit_cents <> b.contribution_profit_cents)
