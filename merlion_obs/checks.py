"""Online guardrail checks, run on every production trace.

They use only what production can see (the trace's spans and merlion_clean), never ground-truth
labels, so they keep working after launch. Each one maps to a landmine from the data audit or to
a rule in architecture.md ("Rules the design enforces") and the harness checklist.

severity: critical = a customer-facing decision may be wrong; warning = degraded or skewed reporting.
"""
import datetime as dt
import json

from .pii import PATTERNS
from .reference import GPS_TO_SGT, MONEY_CAP_SGD, UTC, VERDICTS

LATENCY_BUDGET_MS = 12_000
COST_BUDGET_USD = 0.003


def _spans(tdb, trace_id):
    out = {}
    for name, kind, status, attrs, err in tdb.execute(
            "SELECT name, kind, status, attrs, error FROM spans WHERE trace_id=?", (trace_id,)):
        out.setdefault(name, []).append({"kind": kind, "status": status, "error": err, **json.loads(attrs)})
    return out


def check_trace(tdb, ops, trace):
    tid, resolution, amount, category, dur, cost = trace
    sp = _spans(tdb, tid)
    res = []

    def add(name, passed, sev, detail=""):
        res.append((tid, name, int(passed), sev, detail))

    first = lambda n: (sp.get(n) or [None])[0]
    ro, intake, chat = first("resolveOrder.query"), first("intake") or {}, first("chat") or {}

    # 1. Order numbers repeat across v1/v2 (T01, T26): a collision must be decided on evidence, or asked.
    if ro and ro.get("order.candidates", 0) > 1:
        ok = ro.get("order.basis") in ("scored", "date_filter", "ambiguous")
        add("order_disambiguation", ok, "critical",
            f"{ro['order.candidates']} candidates {ro.get('order.candidate_systems')}, basis={ro.get('order.basis')}: {ro.get('order.reason')}")

    # 2. GPS clock (T16): the GPS window must be centred on the delivery time, read with ts_epoch + 16 h.
    g = first("getGpsTrail")
    if g and g.get("gps.epoch_window"):
        lo, hi = g["gps.epoch_window"]
        centre = dt.datetime.fromtimestamp((lo + hi) / 2, UTC).replace(tzinfo=None) + GPS_TO_SGT
        delivered = dt.datetime.fromisoformat(str(g["gps.delivery_time_sgt"]))
        off_h = (centre - delivered).total_seconds() / 3600
        ok = abs(off_h) <= 1
        add("timezone_alignment", ok, "critical",
            f"GPS window centred {off_h:+.1f} h from the delivery" + ("" if ok else ": ts_epoch read with the wrong offset (audit T16: +16 h)"))

    # 3. Zone comes from the town (T04), not the postal sector: affects the rate and Priya's zone view.
    h = first("getDeliveryHistory")
    chosen = ro and ro.get("order.chosen")
    if h and chosen:
        row = ops.one("SELECT zone, town FROM orders WHERE system_version=? AND order_id=?", tuple(chosen))
        r = first("getRate")
        got = {"history": h.get("delivery.zone"), **({"rate": r.get("rate.zone")} if r and r.get("rate.zone") else {})}
        bad = {k: v for k, v in got.items() if v != row["zone"]}
        add("zone_from_town", not bad, "critical" if "rate" in bad else "warning",
            f"town {row['town']} -> {row['zone']}" + (f"; agent used {bad} from the postal sector" if bad else ""))

    # 4. Conflicts are shown, never resolved silently (T23: POD vs DB remark, COD).
    if chat.get("chat.conflicts_found"):
        ok = chat.get("chat.conflicts_disclosed", 0) >= chat["chat.conflicts_found"]
        add("conflict_disclosed", ok, "critical", f"{chat['chat.conflicts_found']} found, {chat.get('chat.conflicts_disclosed', 0)} shown on the case card")

    # 5. An empty tool result means "the data does not show this" (T24: no rate card before 1 Apr 2026).
    r = first("getRate")
    if r:
        add("honest_gaps", not r.get("rate.guessed"), "critical",
            f"order date {r.get('rate.order_date')}" + (": priced off the Apr 2026 card anyway" if r.get("rate.guessed") else ""))

    # 6. Rejecting a 'not received' claim needs evidence of delivery.
    if resolution == "reject" and category == "not_received":
        pod, gps = first("getPodScan") or {}, g or {}
        ok = bool(pod.get("pod.found")) or bool(gps.get("gps.near_town"))
        add("evidence_grounding", ok, "critical", f"POD found={pod.get('pod.found')}, GPS in town={gps.get('gps.near_town')}")

    # 7. Every verdict carries citations.
    if resolution in VERDICTS:
        n = (first("answer_check") or {}).get("check.citations", 0)
        add("citations_present", n > 0, "critical", f"{n} sources cited")

    # 8. Money out within the cap.
    if resolution in ("refund", "compensate"):
        ok = amount is not None and 0 < amount <= MONEY_CAP_SGD
        add("amount_within_policy", ok, "critical", f"amount={amount} (cap S${MONEY_CAP_SGD:.0f})")

    # 9. Input guardrail: out-of-scope requests are refused deterministically, without an LLM call.
    if intake.get("intake.corporate_ref"):
        llm_calls = sum(1 for ss in sp.values() for s in ss if s["kind"] == "llm")
        add("refusal_without_llm", resolution == "refuse" and llm_calls == 0, "critical",
            f"resolution={resolution}, {llm_calls} LLM calls")

    # 10. No raw PII stored anywhere in the trace.
    blob = json.dumps(sp)
    leaks = sum(len(p.findall(blob)) for p, _ in PATTERNS[:3])
    add("pii_leak", leaks == 0, "critical", f"{leaks} unmasked identifiers")

    # 11-13. Reliability, latency and cost budgets.
    errs = [n for n, ss in sp.items() for s in ss if s["status"] == "error"]
    add("tool_errors", not errs, "warning", ", ".join(errs))
    add("latency_budget", dur <= LATENCY_BUDGET_MS, "warning", f"{dur / 1000:.1f}s (budget {LATENCY_BUDGET_MS / 1000:.0f}s)")
    add("cost_budget", cost <= COST_BUDGET_USD, "warning", f"${cost:.5f} (budget ${COST_BUDGET_USD})")
    return res


def run_checks(tdb, ops, where="1=1", params=()):
    rows = tdb.execute(f"SELECT trace_id, resolution, amount, category, duration_ms, cost_usd FROM traces WHERE {where}", params).fetchall()
    tdb.execute(f"DELETE FROM checks WHERE trace_id IN (SELECT trace_id FROM traces WHERE {where})", params)
    out = []
    for r in rows:
        out.extend(check_trace(tdb, ops, r))
    tdb.executemany("INSERT INTO checks VALUES (?,?,?,?,?)", out)
    tdb.commit()
    return out
