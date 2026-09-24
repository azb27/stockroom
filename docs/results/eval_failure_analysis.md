# Where the agent fails, and why

Written from the traces in `runs/evals/*/traces/` for the runs in [`eval.md`](eval.md). Four failures, each a different lesson. The SQL shown is the agent's own, lightly trimmed.

## 1. The silent empty result → fixed in the product
**First run, Sonnet (T3-07) and Haiku (T3-01/02/04/05):** "How many active FOODS_3 SKUs at CA_1 are out of stock but still selling?"

```sql
... WHERE s.dept = 'FOODS_3' AND s.status = 'active' AND i.on_hand_units = 0
-> [[0]]
```
The data says `'ACTIVE'`. The query was valid, returned nothing, and both models reported **0** with a fluent explanation of why nothing was out of stock. Nothing in the result looked wrong to them.

**Fix:** when `run_sql` returns nothing (or a lone 0), it now checks each text filter against the data:
> *No rows in core.dim_sku have status = 'active'. Text comparisons are case-sensitive: the data uses 'ACTIVE'. Re-run with that value before concluding the answer is 0.*

**Measured:** Haiku went from 92% to 96% overall and from 67% to 87% on multi-step questions (7 fixed, 2 broke). With one run per configuration that is p = 0.18: the right direction, not yet statistically established. Sonnet was 120/120 with and without the hint. In the re-run it didn't repeat the case mistake, which is itself a reminder that single runs are noisy.

## 2. The wrong denominator (still open)
**Haiku with the hint, T3-10:** answered **195** at-risk SKUs; the truth is **44**.

```sql
SELECT sku, SUM(units) AS total_units, COUNT(*) AS day_count ... GROUP BY sku  -- rows = days WITH a sale
... total_units / day_count AS avg_daily_units
```
It averaged over the days that had a sale, not the 28 days asked for. On slow-moving items, which sell on a few days, that inflates daily demand several-fold, so almost every SKU looks at risk. `describe_data` states that a missing row means zero sales; the model read it and still wrote `COUNT(*)`.

**Next fix, not built yet:** steer rate questions to `detect_anomalies`, which computes cover with the right denominator, instead of letting the model derive it in SQL. Alternatively, add a `core.sales_daily_filled` view with explicit zero rows. The general lesson: when a metric has one correct definition, put it in a tool, not in the model's head.

## 3. Noticing isn't fixing (raw data)
**Sonnet on raw data, T1-17:** "What was the shelf price of HOBBIES_1_203 at CA_2 in the week containing 2014-10-27?"

The agent found both `1298.00` and `12.98` in the price book and wrote:
> *The price_book value of $1298.00 … is almost certainly a data-entry error: it's exactly 100x the $12.98 price used in the following week.*

It still reported **1298**. The model had the insight but no authority to override the customer's data mid-answer. The cleaning layer makes that call once, deterministically, and logs it (`core.dq_issues`: 25 prices keyed in cents, ÷100, flagged).

## 4. The failure you can't see (raw data)
**Sonnet on raw data, T1-29:** units of HOUSEHOLD_1_307 at CA_2, 9–15 April 2016. Answered **30**; the truth is **15**.

The agent checked for missing days (none) and summed the POS lines. But that week's POS file was loaded twice. Every line exists twice under a different `load_batch_id`, and at the level of one SKU nothing looks odd: 8 units a day is plausible. The duplicate is only visible in `raw.load_log`, where the same `source_file` appears twice. On raw data the agent went 0/2 on these questions, and 0/6 on monthly revenue, where doubled units and cent-keyed prices compound.

**This is the case for the cleaning layer in one example.** Same model, same questions: 100% with it, 60% on lookups and aggregations without it (p ≈ 10⁻¹³ over all 120).

## What the eval itself got wrong
Six flaws in the questions and scorers, not the agents, were found by reading answers like these. They are logged with before/after effects in [`evals/CORRECTIONS.md`](../../evals/CORRECTIONS.md). The biggest: a question whose true answer depended on the outage days, and a scorer that read *"I must never imply an order was sent"* as a claim that it was.

## Limits of this eval
- **Sonnet 5 scores 100%.** The eval no longer separates it from a better agent. The next version needs harder multi-step questions: chained what-ifs, multi-store reorders, and variance with several drivers of similar size.
- **One run per configuration.** Several answers flipped between runs of the same setup. Repeated runs (e.g. 3 seeds) would turn "direction" claims like the hint's effect into tested ones.
