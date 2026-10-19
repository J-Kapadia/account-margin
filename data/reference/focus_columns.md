# FOCUS columns

The cloud bill (`cloud_focus.csv`) is a column subset of the FinOps Open Cost and Usage
Specification, version 1.0.

| | |
|---|---|
| Specification | FOCUS 1.0 |
| URL | <https://focus.finops.org/docs/specification/v1-0/> |
| Source text | <https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec/tree/v1.0/specification/columns> (tag `v1.0`, one file per column) |
| Retrieved | 2026-10-19 |

`src/margin/focus_import.py` opens with the same URL and date. A change of version or of a quoted
definition is a change to both.

## The sixteen columns

Ten the books carried first, six optional ones added since. `line_id` is not a FOCUS column: the
import numbers each row (`<export file name, lowercased, other characters as _>_<position in the
file>`; the rounding row of a month is `<name>_residual_<YYYY-MM>`).

| # | Column | Role | Specification's description |
|--:|---|---|---|
| 1 | `ChargePeriodStart` | mandatory | "The inclusive start date and time within a charge period." |
| 2 | `ChargePeriodEnd` | mandatory | "The exclusive end date and time of a charge period." |
| 3 | `BilledCost` | mandatory | see below |
| 4 | `EffectiveCost` | mandatory | see below |
| 5 | `BillingCurrency` | mandatory | "Represents the currency that a charge was billed in." |
| 6 | `ServiceName` | mandatory | "An offering that can be purchased from a provider (e.g., cloud virtual machine, SaaS database, professional services from a systems integrator)." |
| 7 | `ServiceCategory` | optional | "Highest-level classification of a service based on the core function of the service." |
| 8 | `ResourceId` | optional | "Identifier assigned to a resource by the provider." |
| 9 | `Tags` | optional | "The set of tags assigned to tag sources that account for potential provider-defined or user-defined tag evaluations." (JSON) |
| 10 | `line_id` | assigned | not in the specification |
| 11 | `ListCost` | optional | see below |
| 12 | `ContractedCost` | optional | see below |
| 13 | `ChargeCategory` | mandatory | "Represents the highest-level classification of a charge based on the nature of how it is billed." |
| 14 | `CommitmentDiscountCategory` | optional | "Indicates whether the commitment-based discount identified in the CommitmentDiscountId column is based on usage quantity or cost (aka "spend")." |
| 15 | `CommitmentDiscountType` | optional | "A provider-assigned identifier for the type of commitment-based discount applied to the row." |
| 16 | `RegionId` | optional | "Provider-assigned identifier for an isolated geographic area where a resource is provisioned or a service is provided." |

Mandatory means the import stops, naming the column, when an export lacks it. An optional column
an export lacks is written empty.

## The cost definitions

Quoted from the specification's column pages (link markup removed). Every cost column is of type
Decimal and is denominated in the BillingCurrency.

- **BilledCost**: "The billed cost represents a charge serving as the basis for invoicing,
  inclusive of the impacts of all reduced rates and discounts while excluding the amortization of
  relevant purchases (one-time or recurring) paid to cover future eligible charges."
- **EffectiveCost**: "Effective Cost represents the amortized cost of the charge after applying
  all reduced rates, discounts, and the applicable portion of relevant, prepaid purchases
  (one-time or recurring) that covered this charge." Also: "EffectiveCost MUST be 0 when
  ChargeCategory is "Purchase" and the purchase is intended to cover future eligible charges."
- **ListCost**: "List Cost represents the cost calculated by multiplying the list unit price and
  the corresponding Pricing Quantity."
- **ContractedCost**: "Contracted Cost represents the cost calculated by multiplying contracted
  unit price and the corresponding Pricing Quantity." Also: "If negotiated discounts are not
  applicable, the Contracted Cost defaults to the List Cost."

## Allowed values read

- `ChargeCategory`: `Usage`, `Purchase`, `Tax`, `Credit`, `Adjustment`.
- `CommitmentDiscountCategory`: `Spend`, `Usage`.
- `ServiceCategory`: the specification's list of nineteen, ending in `Other`; the import's
  rounding row uses `Other`.

## How the import maps an export

- It keeps the columns above, drops every other column of the export and reports their names.
- Tag keys are lowercased and values kept as written; a row with two keys that differ only in case
  and carry different values stops the import.
- Each cost is rounded half-even to two decimals per row. For each month, the difference between
  the cent-rounded exact total of a cost column and the sum of its rounded rows is written as one
  `Adjustment` row (`ServiceName` `Import rounding`, `ServiceCategory` `Other`, tag
  `{"tenant":"shared"}`, no `ResourceId`), so the month's four cost totals equal the export's to
  the cent.
- Nothing is judged: a row the close's controls would fail is written as it is.

## How the books use the cost columns

- The cost basis is `EffectiveCost`; `BillingCurrency` must be `USD`.
- A row is in month M when `ChargePeriodStart` is at or after the first instant of M (UTC) and
  `ChargePeriodEnd` is at or before the first instant of M+1; the end is exclusive, as in the
  specification. Both timestamps are UTC with a `Z` suffix.
- Every cost carries at most two decimals.
