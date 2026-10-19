"""The duplicate invoice line: a copy of one March invoice line, booked twice."""

ERROR = "duplicate_invoice"
TITLE = "A duplicate invoice line"
CONTROL = "CTL-REV-DUP"
PLANTED = (
    "A copy of one March invoice line was appended to the invoice export with a new line id, the original "
    "id suffixed with a marker, and the same account, service period, line type, model and amount. Nothing "
    "else changed, so the account's revenue counts the line twice."
)
CAUGHT = (
    "The control checks two grains over the month's invoice lines: invoice and line id are unique, and the "
    "natural key of account, service period, line type, model and amount is unique. It returns the "
    "duplicated key with the revenue number it feeds and every row behind it, each with its row hash. The "
    "revenue tie-out also fails, because the account's revenue now exceeds the ledger."
)
REMEDIATION = [
    "Remove the re-entered line from the invoice export, keeping the original line id.",
    "Confirm the ledger carries the invoice once.",
    "Re-run margin close --month 2026-03.",
    "Confirm CTL-REV-DUP and CTL-TIE-REV pass.",
]
