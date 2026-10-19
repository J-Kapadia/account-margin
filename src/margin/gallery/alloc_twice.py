"""The double-counted cloud charge: one March cloud-bill row both a tenant's direct cost and part of the shared pool."""

ERROR = "alloc_twice"
TITLE = "A shared cloud charge counted as direct too"
CONTROL = "CTL-ALLOC-CONSERVE"
PLANTED = (
    "A copy of one untagged March cloud-bill row was appended with a tenant tag naming an account. The same "
    "charge is now that account's direct cost and also part of the shared pool that is spread over everyone, "
    "so it is counted twice."
)
CAUGHT = (
    "The control requires every pool to allocate in full and nothing to be counted twice. It returns a charge "
    "with the same resource and charge period present both tagged and untagged, naming the account's direct "
    "cloud number and both rows. The cloud tie-out fails behind it, because cloud cost now exceeds the ledger."
)
REMEDIATION = [
    "Decide which row is right: if the charge belongs to the tenant, remove the untagged copy from the shared "
    "pool, otherwise drop the tagged copy.",
    "Confirm the cloud bill carries the charge once.",
    "Re-run margin close --month 2026-03.",
    "Confirm CTL-ALLOC-CONSERVE and CTL-TIE-CLD pass.",
]
