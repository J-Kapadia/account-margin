# Hand-worked reference for the token-price scenario

This is the reference `margin scenario --check` compares against. Every figure below is worked
with Python's `decimal` module (28 digits, round half to even) from two inputs only: the base-month
values of the three `scenario` preset accounts, and `data/reference/list_price_series.csv`. The
product is not run to produce the arithmetic; its printed base-month values are the starting point.
The machine-readable copies are `expected.csv` and `summary.json` in this directory.

**Re-work this document after any price change.** The preset is generated on demand with
`data/reference/model_prices.csv`, so a change to that file moves the base-month values, and a
change to `list_price_series.csv` moves the rate and the factors. Either one makes the check fail
until the base-month values, `expected.csv` and `summary.json` are worked again from the steps below.

## 1. Base month 2026-03

The preset is `margin gen --fixture scenario --out <dir>/books --config tests/fixtures/config`, closed
with `tests/fixtures/config` and the committed sign-off. The components are those printed by
`margin explain cp/2026-03/<account>/<component>` on the closed warehouse (cents):

| account | revenue | inference_cost | cloud_direct | cloud_shared | support_cost | contribution_profit | margin_pct |
|---|---:|---:|---:|---:|---:|---:|---:|
| acct_0001 | 152927 | 127223 | 0 | 1333 | 0 | 24371 | 15.9364 |
| acct_0002 | 730103 | 231176 | 0 | 41421 | 343350 | 114156 | 15.6356 |
| acct_0003 | 309860 | 131466 | 111378 | 8143 | 0 | 58873 | 18.9999 |

The floor is 30 percent (`floor_pct` in `tests/fixtures/config/policy.yaml`). Contribution profit is
revenue minus inference, direct cloud, shared cloud and support cost; an account is below the floor when
`contribution_profit * 100 < 30 * revenue`. Check of the base month:

- acct_0001: 152927 - 127223 - 0 - 1333 - 0 = 24371; 24371 x 100 / 152927 = 15.9364; 2437100 < 4587810 is true, so it is below the floor
- acct_0002: 730103 - 231176 - 0 - 41421 - 343350 = 114156; 114156 x 100 / 730103 = 15.6356; 11415600 < 21903090 is true, so it is below the floor
- acct_0003: 309860 - 131466 - 111378 - 8143 - 0 = 58873; 58873 x 100 / 309860 = 18.9999; 5887300 < 9295800 is true, so it is below the floor

## 2. The blended price and the rate

A tier's price in a period is (3 x input + output) / 4 (USD per million tokens); the blend is the
unweighted mean of the three tiers, rounded half-even to six places.

| period | large in / out | medium in / out | small in / out | large price | medium price | small price | blend |
|---|---|---|---|---|---|---|---|
| 2026-03 | 5.00 / 25.00 | 1.75 / 14.00 | 1.00 / 5.00 | 10.00 | 4.8125 | 2.00 | 5.604167 |
| 2026-09 | 5.00 / 25.00 | 2.00 / 12.00 | 1.00 / 5.00 | 10.00 | 4.50 | 2.00 | 5.500000 |

The 2026-03 and 2026-09 rows of the series are the only ones the arithmetic reads. The other periods
must exist and be sourced (the check validates them), but they do not enter the figures.

- blend_first = (10.00 + 4.8125 + 2.00) / 3 = 5.604167
- blend_last = (10.00 + 4.50 + 2.00) / 3 = 5.500000
- ratio = 5.500000 / 5.604167 = 0.9814125810312219460983229086
- computed_change = ratio - 1 = -0.0185874189687780539016770914
- monthly_rate = ratio^(1/6) - 1 = -0.0031221718419264653772692361 (six months between 2026-03 and 2026-09)

```
python3 -c "from decimal import *; c=Context(prec=28,rounding=ROUND_HALF_EVEN)\
; r=c.divide(Decimal('5.500000'),Decimal('5.604167'))\
; print(c.subtract(r,1), c.subtract(c.power(r,c.divide(1,6)),1))"
```

## 3. The factor, the scaled cost and the margin path

The factor k months after the base month, for persistence p (full 1, half 0.5, flat 0), is
(1 + p x rate)^k, to 28 digits. The scaled inference cost is base inference x factor, rounded
half-even to whole cents. Contribution profit and margin are then worked as in section 1 with the
scaled inference cost in place of the base one. k = 3, 6, 9, 12 are the ends of Q1..Q4.

### Case full (p = 1)

1 + p x rate = 0.9968778281580735346227307639

**acct_0001** (base inference 127223; revenue 152927; fixed costs 0 + 1333 + 0):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 0.9906626979104552648101749001 | 126035.0804162618501549448813 | 126035 | 25559 | 16.7132 | 2555900 < 4587810 is true | false |
| Q2 | 6 | 0.9814125810312219460983229083 | 124858.2527965351496484669354 | 124858 | 26736 | 17.4829 | 2673600 < 4587810 is true | false |
| Q3 | 9 | 0.9722488352876536256781716085 | 123692.4135718011572196540265 | 123692 | 27902 | 18.2453 | 2790200 < 4587810 is true | false |
| Q4 | 12 | 0.9631706542063647824093707404 | 122537.4601400963467124673737 | 122537 | 29057 | 19.0006 | 2905700 < 4587810 is true | false |

Recovery quarter: none.

**acct_0002** (base inference 231176; revenue 730103; fixed costs 0 + 41421 + 343350):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 0.9906626979104552648101749001 | 229017.4398521474062977569927 | 229017 | 116315 | 15.9313 | 11631500 < 21903090 is true | false |
| Q2 | 6 | 0.9814125810312219460983229083 | 226879.0348324737646112258966 | 226879 | 118453 | 16.2241 | 11845300 < 21903090 is true | false |
| Q3 | 9 | 0.9722488352876536256781716085 | 224760.5967464586145697769998 | 224761 | 120571 | 16.5142 | 12057100 < 21903090 is true | false |
| Q4 | 12 | 0.9631706542063647824093707404 | 222661.9391568105849382686903 | 222662 | 122670 | 16.8017 | 12267000 < 21903090 is true | false |

Recovery quarter: none.

**acct_0003** (base inference 131466; revenue 309860; fixed costs 111378 + 8143 + 0):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 0.9906626979104552648101749001 | 130238.4622434959118435344534 | 130238 | 60101 | 19.3962 | 6010100 < 9295800 is true | false |
| Q2 | 6 | 0.9814125810312219460983229083 | 129022.3863778506243657621195 | 129022 | 61317 | 19.7886 | 6131700 < 9295800 is true | false |
| Q3 | 9 | 0.9722488352876536256781716085 | 127817.6653799266715534065087 | 127818 | 62521 | 20.1772 | 6252100 < 9295800 is true | false |
| Q4 | 12 | 0.9631706542063647824093707404 | 126624.1932258939524842303338 | 126624 | 63715 | 20.5625 | 6371500 < 9295800 is true | false |

Recovery quarter: none.

### Case half (p = 0.5)

1 + p x rate = 0.9984389140790367673113653820

**acct_0001** (base inference 127223; revenue 152927; fixed costs 0 + 1333 + 0):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 0.9953240494005185789884090975 | 126628.1115368821751746423706 | 126628 | 24966 | 16.3254 | 2496600 < 4587810 is true | false |
| Q2 | 6 | 0.9906699633150459486373544661 | 126036.0047428300907234901472 | 126036 | 25558 | 16.7125 | 2555800 < 4587810 is true | false |
| Q3 | 9 | 0.9860376395061947221722192536 | 125446.6666108966111389162501 | 125447 | 26147 | 17.0977 | 2614700 < 4587810 is true | false |
| Q4 | 12 | 0.9814269762146344856589896485 | 124860.0841949544431689936401 | 124860 | 26734 | 17.4815 | 2673400 < 4587810 is true | false |

Recovery quarter: none.

**acct_0002** (base inference 231176; revenue 730103; fixed costs 0 + 41421 + 343350):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 0.9953240494005185789884090975 | 230095.0324442142830162244615 | 230095 | 115237 | 15.7837 | 11523700 < 21903090 is true | false |
| Q2 | 6 | 0.9906699633150459486373544661 | 229019.1194393190622221890561 | 229019 | 116313 | 15.9310 | 11631300 < 21903090 is true | false |
| Q3 | 9 | 0.9860376395061947221722192536 | 227948.2373504840710928849582 | 227948 | 117384 | 16.0777 | 11738400 < 21903090 is true | false |
| Q4 | 12 | 0.9814269762146344856589896485 | 226882.3626533943418567025910 | 226882 | 118450 | 16.2237 | 11845000 < 21903090 is true | false |

Recovery quarter: none.

**acct_0003** (base inference 131466; revenue 309860; fixed costs 111378 + 8143 + 0):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 0.9953240494005185789884090975 | 130851.2714784885755052901904 | 130851 | 59488 | 19.1983 | 5948800 < 9295800 is true | false |
| Q2 | 6 | 0.9906699633150459486373544661 | 130239.4173971758306835584422 | 130239 | 60100 | 19.3959 | 6010000 < 9295800 is true | false |
| Q3 | 9 | 0.9860376395061947221722192536 | 129630.4243153213953450929764 | 129630 | 60709 | 19.5924 | 6070900 < 9295800 is true | false |
| Q4 | 12 | 0.9814269762146344856589896485 | 129024.2788550331372916447331 | 129024 | 61315 | 19.7880 | 6131500 < 9295800 is true | false |

Recovery quarter: none.

### Case flat (p = 0)

1 + p x rate = 1.000000000000000000000000000

**acct_0001** (base inference 127223; revenue 152927; fixed costs 0 + 1333 + 0):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 1.000000000000000000000000000 | 127223.0000000000000000000000 | 127223 | 24371 | 15.9364 | 2437100 < 4587810 is true | false |
| Q2 | 6 | 1.000000000000000000000000000 | 127223.0000000000000000000000 | 127223 | 24371 | 15.9364 | 2437100 < 4587810 is true | false |
| Q3 | 9 | 1.000000000000000000000000000 | 127223.0000000000000000000000 | 127223 | 24371 | 15.9364 | 2437100 < 4587810 is true | false |
| Q4 | 12 | 1.000000000000000000000000000 | 127223.0000000000000000000000 | 127223 | 24371 | 15.9364 | 2437100 < 4587810 is true | false |

Recovery quarter: none.

**acct_0002** (base inference 231176; revenue 730103; fixed costs 0 + 41421 + 343350):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 1.000000000000000000000000000 | 231176.0000000000000000000000 | 231176 | 114156 | 15.6356 | 11415600 < 21903090 is true | false |
| Q2 | 6 | 1.000000000000000000000000000 | 231176.0000000000000000000000 | 231176 | 114156 | 15.6356 | 11415600 < 21903090 is true | false |
| Q3 | 9 | 1.000000000000000000000000000 | 231176.0000000000000000000000 | 231176 | 114156 | 15.6356 | 11415600 < 21903090 is true | false |
| Q4 | 12 | 1.000000000000000000000000000 | 231176.0000000000000000000000 | 231176 | 114156 | 15.6356 | 11415600 < 21903090 is true | false |

Recovery quarter: none.

**acct_0003** (base inference 131466; revenue 309860; fixed costs 111378 + 8143 + 0):

| quarter | k | factor (28 digits) | inference x factor | inference (cents) | contribution | margin_pct | test | above_floor |
|---|---:|---|---|---:|---:|---:|---|---|
| Q1 | 3 | 1.000000000000000000000000000 | 131466.0000000000000000000000 | 131466 | 58873 | 18.9999 | 5887300 < 9295800 is true | false |
| Q2 | 6 | 1.000000000000000000000000000 | 131466.0000000000000000000000 | 131466 | 58873 | 18.9999 | 5887300 < 9295800 is true | false |
| Q3 | 9 | 1.000000000000000000000000000 | 131466.0000000000000000000000 | 131466 | 58873 | 18.9999 | 5887300 < 9295800 is true | false |
| Q4 | 12 | 1.000000000000000000000000000 | 131466.0000000000000000000000 | 131466 | 58873 | 18.9999 | 5887300 < 9295800 is true | false |

Recovery quarter: none.

## 4. Recovery quarters

The recovery quarter of an account is the first quarter whose last month is above the floor, or `none`.

| case | acct_0001 | acct_0002 | acct_0003 |
|---|---|---|---|
| full | none | none | none |
| half | none | none | none |
| flat | none | none | none |

