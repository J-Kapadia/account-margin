"""The one-cent slip: one March ledger entry under the inference cost code raised by a single cent."""

ERROR = "rounding_slip"
TITLE = "A one-cent slip in the ledger"
CONTROL = "CTL-TIE-INF"
PLANTED = (
    "One March ledger entry under the inference cost code was raised by a single cent, the kind of difference "
    "a rounding step or a hand edit leaves."
)
CAUGHT = (
    "No control was written for this error. The inference tie-out requires inference cost summed over accounts "
    "to equal the ledger total for the code to the cent, and it alone fails, naming the ledger number for the "
    "code and the rows behind it. That is what makes the tie-outs the backstop for every other error."
)
REMEDIATION = [
    "Find the entry whose amount differs from the provider invoice it books.",
    "Correct it in March while March is open, or post a one-cent adjusting entry in the open month once March "
    "is locked.",
    "Re-run margin close --month 2026-03.",
    "Confirm CTL-TIE-INF passes.",
]
