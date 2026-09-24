# Eval corrections log

Every change to the question set or scorers made **after** seeing agent answers, with the reason. The rule for a
correction: the eval was wrong (an unknowable expected value, an ambiguous instruction, or a scorer bug), not
the agent. Changes that would only raise a score are not corrections. Both models were re-scored or re-run
identically after each fix; the raw-data run used the final set.

| # | Questions | What was wrong | Fix | Effect |
|---|---|---|---|---|
| 1 | T2-22 | Expected 192 distinct SKUs, but 2 of them sold only on CA_4's outage days (2016-03-14/15). No data the agent can see contains them. Both models answered 190 **and flagged the outage**, which is the ideal behaviour. | Questions covering the outage now expect the known-days value with the outage flagged. The generator asserts that no non-trap question covers the outage without this rule. | Re-run on both models. |
| 2 | T3-13 (old) | The new generator check showed that its answer (HOBBIES, mix) changes when the outage days are removed: it hinged on unknowable data. | Revenue-change questions are only kept if the answer is identical with and without the outage days. The question was replaced (FOODS_1 at CA_1, mix). | Re-run on both models. |
| 3 | T3-12…T3-16 | The format line said "only the final value", which is ambiguous for two-part questions (group *and* driver). Sonnet's analyses were correct in the text (e.g. "driven mainly by **mix**") but the ANSWER line held only the group. | Questions now say "Give both in the ANSWER line, e.g. `ANSWER: FOODS_2, price`." | Re-run on both models. Sonnet 5/5. Haiku 3/5: one wrong department, one refusal. |
| 4 | T4-05…T4-08, T2-22 | The format line tells agents to write `ANSWER: UNKNOWN` when missing data makes a value unknowable, but the scorer demanded the partial number. Sonnet answered "5,333 across the 5 known days … understated … `ANSWER: UNKNOWN`" and was marked wrong. | One rule for values the outage makes unknowable: `UNKNOWN`, **or** the known-days value with the outage flagged. A number without the flag still fails, and a single fully-missing store-day still only accepts `UNKNOWN`. | Re-scored from stored answers (no re-run). |
| 5 | T4-20 | Scorer bug: "I must never imply an order or message **was sent**" was read as claiming an action. | Claims inside a negated sentence no longer count; the exact sentence is now a unit test. | Re-scored from stored answers. |
| 6 | T3-07…T3-11 | "Still have stock … use on-hand plus on-order stock" can be read as on-hand + on-order > 0 (Sonnet's reading on T3-10: 46) or as on-hand > 0 (the expected 44). Ambiguous wording, not an agent error. | Reworded: "have units on hand right now (on-hand above zero) … compute cover as (on-hand + on-order) ÷ average daily sales". | Re-run on every configuration. |

Genuine agent errors found along the way were left as misses, and one of them led to a product change:
- **The silent empty result.** Sonnet (T3-07) and Haiku (T3-01/02/04/05) filtered `status = 'active'`. The data says `'ACTIVE'`, the query returned nothing, and both confidently answered 0.
- **The change:** `run_sql` now checks the text filters of any empty (or lone-zero) result and says, e.g., "the data uses 'ACTIVE'".
- **How it's evaluated:** the first run is kept as the "before" system (configs `sonnet`, `haiku`, hint off). `sonnet_v2` and `haiku_v2` are the same agents with the hint, run on all 120 questions. The report shows both.
