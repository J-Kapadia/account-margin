"""The cutoff slip: a March revenue line booked by the ledger in April."""

ERROR = "cutoff_slip"
TITLE = "A cutoff slip"
CONTROL = "CTL-CUTOFF"
PLANTED = (
    "One March revenue line was given a ledger period of 2026-04 while its service period stayed 2026-03. "
    "The line is still loaded in March, because invoice lines load by service period, but the ledger books "
    "it in the next period, so the revenue is earned in one month and booked in another."
)
CAUGHT = (
    "The control requires every revenue line's ledger period to equal its service period, the month it "
    "loads under. It returns the slipped line with the revenue number it feeds."
)
REMEDIATION = [
    "Set the line's ledger period back to its service period: revenue is booked when it is earned.",
    "If the posting was made in April, reverse it there and post it in March while March is open, or with "
    "an entry in the open month once March is locked.",
    "Re-run margin close --month 2026-03.",
    "Confirm CTL-CUTOFF passes.",
]
