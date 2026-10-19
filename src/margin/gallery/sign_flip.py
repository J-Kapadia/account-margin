"""The flipped credit: one March credit line with its amount negated, so a credit adds to revenue."""

ERROR = "sign_flip"
TITLE = "A credit with its sign flipped"
CONTROL = "CTL-SIGN"
PLANTED = (
    "One March credit line had its amount negated. A credit that should lower the account's revenue now "
    "raises it."
)
CAUGHT = (
    "The control requires every amount to carry the sign of its kind: credits at or below zero, charges at or "
    "above zero, cost ledger entries at or above zero. It returns the offending line with the revenue number "
    "it feeds. The revenue tie-out fails behind it, because account revenue now exceeds the ledger."
)
REMEDIATION = [
    "Restore the credit's sign in the invoice export.",
    "Check that the credit was not also reissued as a charge.",
    "Re-run margin close --month 2026-03.",
    "Confirm CTL-SIGN and CTL-TIE-REV pass.",
]
