# Reference data

Small, hand-checked tables that the close loads into the warehouse as `raw.ref_<file>`.

## `list_price_series.csv`

Vendor list prices per million tokens, one row per tier and month, 2026-03 through 2026-09
(21 rows). It feeds the token-price what-if; it is a blend of vendors' public list prices, not any
company's own price or cost.

| Column | Meaning |
|---|---|
| `tier` | `large`, `medium` or `small` |
| `model` | the tier's current model in that month (the vendor's API name) |
| `period` | the month, `YYYY-MM` |
| `input_usd_per_mtok`, `output_usd_per_mtok` | USD per million input and output tokens, two decimals |
| `source_url` | the Internet Archive capture of the vendor's pricing page that was read |
| `retrieved_on` | the day that capture was read |
| `notes` | the capture times, the confirming capture, the price tier used, and any model change |

Tiers: `large` is Anthropic's Opus line, `medium` is OpenAI's mainline GPT model at the medium price
point (the line gpt-5.2 belongs to), `small` is Anthropic's Haiku line.

Method:

- A tier's current model in a month is the newest generally available model of its line on the
  vendor's own pricing page (not preview, limited availability or deprecated), as archived by the
  Internet Archive in the last capture at or before 23:59:59 UTC on the 1st of the month. The next
  capture must list the same model at the same price. A model that appears later in the month
  enters the next month.
- The price is the standard on-demand rate: uncached base input, default endpoint, base context
  tier. Batch, flex, fast or priority processing, cache reads and writes, long-context premiums and
  data-residency uplifts are left out.
- The blended price of a month is the unweighted mean over the three tiers of
  (3 × input + output) / 4; the change over the series is blend(2026-09) / blend(2026-03) − 1.
- A model's price can fall or rise only through a new model, since vendors rarely reprice an
  existing one, so the series follows the tier rather than a fixed model.
