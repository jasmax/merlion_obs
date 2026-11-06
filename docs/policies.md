# Assumed resolution policy

The client has not given a written policy for re-deliver, refund, compensate or reject (Build Guide, "Data problems we must handle"). Until Priya sends hers, the app and this harness use the rules below. Every verdict says **"based on assumed policy"**.

The rules are coded once, in `merlion_obs/reference.py` (`policy()`), and that code labels the synthetic golden set and scores the chat step. If a rule changes, change it there and here together. Copy this file to the team repo as `docs/policies.md`, which the Build Guide plans for.

| Complaint | Facts the verdict needs | Rule |
|---|---|---|
| Not received | Delivery outcome; POD scan and GPS check | No successful delivery on record: **re-deliver**. Delivered, and a POD scan names who received it or a GPS ping puts the driver in the delivery town within 30 min: **reject**. Delivered with no POD or GPS evidence: **re-deliver** (benefit of the doubt). |
| Late | `deliveries.was_late` | Late: **compensate** with a S$5.00 voucher. On time: **reject**. |
| Damaged | Delivery outcome | Delivered: **compensate** S$15.00 goodwill. Not delivered yet: **re-deliver**. |
| Overcharged | Rate card price for the order's zone, weight band and date, incl. 9% GST; the amount charged | Charged above the price: **refund** the difference. Otherwise: **reject**. |

What happens outside the four verdicts:

- **Clarify** (ask the CS agent). The email gives no single order number, or `resolveOrder` cannot tell v1 from v2 (audit T26). No LLM call is made.
- **Refuse.** Corporate-account requests (`MERL-C-xxxx`, audit T27) are refused by code, with no LLM call. No table holds corporate accounts.
- **Escalate** (hand to a human). Two situations:
  - A fact the verdict needs is missing. Examples: no rate card for orders before 1 Apr 2026 (T24), or no amount charged in the email. The app says "the data does not show this" and never guesses.
  - The LLM is unavailable, or the answer check holds the answer.
- **Money cap.** Any refund or compensation above S$30.00 goes to a human.

Assumptions behind the facts, all listed in the data quality report:

- The zone comes from the town in the address (`orders.zone`, audit T04).
- GPS Singapore time = `ts_epoch` + 16 h (T16).
- "In the delivery town" means within 2 km of the town centre, because the cleaned schema has no geocode for an address. The town centres are in `merlion_obs/reference.py`.
