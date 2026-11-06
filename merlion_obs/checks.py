"""Online guardrail checks, run on every production trace.

They use only what production can see (the trace's spans and the ops DB), never
ground-truth labels, so they keep working after launch. Each one targets a
failure mode from the client brief or a basic safety/cost guarantee.

severity: critical = a customer-facing decision may be wrong; warning = degraded.
"""
import datetime as dt
import json
import re

from .data import _dist_m
from .tracer import _PII

F = "%Y-%m-%dT%H:%M:%SZ"
LATENCY_BUDGET_MS = 12_000
COST_BUDGET_USD = 0.03
DAMAGE_CAP = 100.0


def _spans(tdb, trace_id):
    out = {}
    for name, status, attrs, err in tdb.execute(
            "SELECT name, status, attrs, error FROM spans WHERE trace_id=?", (trace_id,)):
        out.setdefault(name, []).append({"status": status, "error": err, **json.loads(attrs)})
    return out


def _near_pings(ops, driver, center_utc, lat, lng, minutes=30, radius=150):
    t0, t1 = center_utc - dt.timedelta(minutes=minutes), center_utc + dt.timedelta(minutes=minutes)
    rows = ops.execute("SELECT lat, lng FROM gps_pings WHERE driver_id=? AND ts_utc BETWEEN ? AND ?",
                       (driver, t0.strftime(F), t1.strftime(F))).fetchall()
    return sum(1 for p in rows if _dist_m(*p, lat, lng) <= radius)


def check_trace(tdb, ops, trace):
    tid, resolution, amount, category, dur, cost = trace
    sp = _spans(tdb, tid)
    res = []

    def add(name, passed, sev, detail=""):
        res.append((tid, name, int(passed), sev, detail))

    # 1. Order-number collision across v1/v2 must be explicitly disambiguated.
    ro = (sp.get("resolve_order") or [{}])[0]
    if ro.get("order.candidates", 0) > 1:
        ok = ro.get("order.basis") in ("email+date", "ambiguous")
        add("order_disambiguation", ok, "critical",
            f"{ro.get('order.candidates')} candidates {ro.get('order.candidate_systems')}, basis={ro.get('order.basis')}")

    # 2. GPS window must line up with the delivery time (naive SGT vs UTC trap).
    g = (sp.get("get_gps_trail") or [None])[0]
    if g:
        order_no = ro.get("order.query")
        lat, lng, delivered = ops.execute(
            "SELECT addr_lat, addr_lng, delivered_at FROM orders_v2 WHERE order_no=?", (order_no,)).fetchone()
        centre = dt.datetime.strptime(g["gps.window_start_utc"], F) + dt.timedelta(minutes=30)
        here = _near_pings(ops, g["gps.driver_id"], centre, lat, lng)
        shifted = max(_near_pings(ops, g["gps.driver_id"], centre + dt.timedelta(hours=h), lat, lng) for h in (-8, 8))
        ok = not (here == 0 and shifted >= 2)
        add("timezone_alignment", ok, "critical",
            f"pings at address: window={here}, window±8h={shifted}"
            + ("" if ok else " -> timestamps read in the wrong timezone"))

    # 3. Postal code must be a valid 6-digit SG code, or the agent must not rely on it.
    dh = (sp.get("get_delivery_history") or [{}])[0]
    if "delivery.postal_valid" in dh:
        code = dh.get("delivery.postal_code", "")
        bad = not dh["delivery.postal_valid"]
        ok = not bad or dh.get("delivery.geocode") == "address_fallback"
        add("postal_code_valid", ok, "warning",
            f"postal={code!r} ({len(code)} digits), geocode={dh.get('delivery.geocode')}, zone={dh.get('delivery.zone')}")

    # 4. A rejection must be backed by evidence.
    if resolution == "reject" and category in ("not_received", "damaged"):
        pod = (sp.get("get_pod_scan") or [{}])[0]
        ok = bool(pod.get("pod.found"))
        if category == "not_received":
            ok = ok and g is not None and (g.get("gps.min_dist_m") or 1e9) <= 100
        add("evidence_grounding", ok, "critical", "reject requires POD" + (" + GPS at address" if category == "not_received" else ""))

    # 5. Money out must be within policy caps.
    if resolution in ("refund", "compensate"):
        ok = amount is not None and 0 < amount <= DAMAGE_CAP
        add("amount_within_policy", ok, "critical", f"amount={amount}")

    # 6. No raw PII persisted anywhere in the trace.
    blob = json.dumps(sp)
    leaks = sum(len(p.findall(blob)) for p, _ in _PII)
    add("pii_leak", leaks == 0, "critical", f"{leaks} unredacted identifiers")

    # 7–9. Reliability, latency and cost budgets.
    errs = [n for n, ss in sp.items() for s in ss if s["status"] == "error"]
    add("tool_errors", not errs, "warning", ", ".join(errs))
    add("latency_budget", dur <= LATENCY_BUDGET_MS, "warning", f"{dur / 1000:.1f}s (budget {LATENCY_BUDGET_MS / 1000:.0f}s)")
    add("cost_budget", cost <= COST_BUDGET_USD, "warning", f"${cost:.4f} (budget ${COST_BUDGET_USD})")
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
