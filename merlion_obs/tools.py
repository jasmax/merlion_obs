"""Python twins of the Spring tools in the Build Guide (backend/*/...Tools.java).

Each tool runs one fixed query against merlion_clean and returns a ToolResult:
    {"data": ..., "sources": ["deliveries#73", ...], "conflicts": [...], "gaps": [...]}
`sources` become the citations on the case card; `gaps` are the "the data does not show this"
answers; `conflicts` are two sources that disagree (never resolved silently).

Versions reproduce the bugs the harness has to catch:
  resolveOrder v1   takes the newest system on an order-number collision (audit T01/T26)
               v2   filters by order date, scores phone + name + date, asks when tied
  tools        v1   getGpsTrail reads ts_epoch as true UTC (+8 h): 8 h off (audit T16)
                    getRate takes the zone from the postal sector (audit T04)
               v2   getGpsTrail uses the ingested `ts` (UTC + 16 h)
               v3   getRate also takes the zone from orders.zone (town)
"""
import datetime as dt
import re

from .reference import (GPS_NEAR_M, GPS_TO_SGT, GPS_WINDOW, GST, NEEDS, POLICY_SOURCE, RATE_CARD_START,
                        TOWN_CENTROID, UTC, dist_m, zone_from_postal_sector)

TOOL_VERSIONS = {
    "v1": {"gps_epoch_plus_8": True, "zone_from_postal": True},
    "v2": {"gps_epoch_plus_8": False, "zone_from_postal": True},
    "v3": {"gps_epoch_plus_8": False, "zone_from_postal": False},
}
ROLE = re.compile(r"\b(neighbou?r|security|guard|front desk|concierge|reception|mailroom|colleague)\b", re.I)
COD_COLLECTED = re.compile(r"COD collected \$?\s*(\d+(?:\.\d{1,2})?)", re.I)


def result(data=None, sources=(), conflicts=(), gaps=()):
    return {"data": data, "sources": list(sources), "conflicts": list(conflicts), "gaps": list(gaps)}


def _epoch(naive_utc):
    return int(naive_utc.replace(tzinfo=UTC).timestamp())


# ---------------------------------------------------------------- resolveOrder (runs before the LLM)
def resolve_order(ops, order_id, clues, received_at, version="v2"):
    rows = ops.all(
        "SELECT o.system_version, o.order_id, o.order_date, o.customer_name, o.customer_phone, o.status, "
        "MAX(d.delivered_time) AS delivered_time FROM orders o LEFT JOIN deliveries d "
        "ON d.system_version=o.system_version AND d.order_id=o.order_id WHERE o.order_id=? "
        "GROUP BY o.system_version, o.order_id, o.order_date, o.customer_name, o.customer_phone, o.status", (order_id,))
    srcs = [f"orders ({r['system_version']}) #{r['order_id']}" for r in rows]
    out = {"candidates": len(rows), "candidate_systems": sorted(r["system_version"] for r in rows)}
    if not rows:
        return result({**out, "status": "NOT_FOUND", "reason": f"order {order_id} is in neither v1 nor v2"}, srcs)
    if version == "v1":
        r = max(rows, key=lambda r: r["system_version"])
        return result({**out, "status": "RESOLVED", "chosen": [r["system_version"], r["order_id"]],
                       "basis": "single_match" if len(rows) == 1 else "newest_system",
                       "reason": "only match" if len(rows) == 1 else "took the newer system (v2)"}, srcs)
    day = received_at.date()
    live = [r for r in rows if r["order_date"] is None or r["order_date"] <= day]
    if not live:
        return result({**out, "status": "NOT_FOUND", "reason": "every candidate was ordered after the email"}, srcs)
    phone = re.sub(r"\D", "", clues.get("phone") or "")
    name = (clues.get("name") or "").lower()
    hint = clues.get("date_hint") or {}

    def score(r):
        s, why = 0, []
        if phone and re.sub(r"\D", "", r["customer_phone"] or "")[-8:] == phone[-8:]:
            s, why = s + 3, why + ["phone"]
        if name and r["customer_name"] and name == r["customer_name"].lower():
            s, why = s + 2, why + ["name"]
        ref = r["delivered_time"] or (r["order_date"] and dt.datetime.combine(r["order_date"], dt.time()))
        if ref and hint.get("month") == ref.month and (hint.get("day") is None or abs(hint["day"] - ref.day) <= 3):
            s, why = s + 1, why + ["date"]
        return s, why

    scored = sorted(((*score(r), r) for r in live), key=lambda x: -x[0])
    out["scores"] = {f"{r['system_version']}": s for s, _, r in scored}
    if len(live) == 1:
        r = live[0]
        return result({**out, "status": "RESOLVED", "chosen": [r["system_version"], r["order_id"]], "basis": "date_filter",
                       "reason": "only candidate ordered before the email" + (f" (+{', '.join(scored[0][1])})" if scored[0][1] else "")}, srcs)
    (s1, why1, r1), (s2, _, _) = scored[0], scored[1]
    if s1 > 0 and s1 > s2:
        return result({**out, "status": "RESOLVED", "chosen": [r1["system_version"], r1["order_id"]], "basis": "scored",
                       "reason": f"{r1['system_version']} matches on {', '.join(why1)}"}, srcs)
    return result({**out, "status": "AMBIGUOUS", "basis": "ambiguous",
                   "reason": "v1 and v2 both fit equally: ask the CS agent which order"}, srcs)


# ---------------------------------------------------------------- tools the LLM may call
def get_delivery_history(ops, sv, oid):
    order = ops.one("SELECT system_version, order_id, order_date, town, zone, postal_code_clean, parcel_weight_kg, "
                    "cod_amount, promised_date, status FROM orders WHERE system_version=? AND order_id=?", (sv, oid))
    rows = ops.all("SELECT d.delivery_id, d.delivered_time, d.outcome, d.was_late, d.remarks, dr.driver_code "
                   "FROM deliveries d LEFT JOIN drivers dr ON dr.driver_id=d.driver_id "
                   "WHERE d.system_version=? AND d.order_id=? ORDER BY d.delivered_time", (sv, oid))
    srcs = [f"orders ({sv}) #{oid}"] + [f"deliveries#{r['delivery_id']}" for r in rows]
    gaps = [] if rows else [f"no delivery attempt on record (order status {order and order['status']})"]
    last = rows[-1] if rows else None
    return result({"order": order, "attempts": rows, "last": last}, srcs, gaps=gaps)


def get_pod_scan(ops, sv, oid, history=None):
    if sv == "v2":
        return result(None, gaps=["no POD scan on file: v2 orders have none (audit T21)"])
    pod = ops.one("SELECT pod_id, source_file, pod_datetime, driver_code, received_by, pod_remarks, has_signature "
                  "FROM pod_extracts WHERE system_version=? AND order_id=?", (sv, oid))
    if not pod:
        return result(None, gaps=["no POD scan on file for this order"])
    conflicts, last = [], (history or {}).get("last")
    if last and (last.get("remarks") or "") != (pod.get("pod_remarks") or ""):
        conflicts.append({"field": "remark", "a": f"deliveries#{last['delivery_id']}", "a_says": last.get("remarks"),
                          "b": pod["source_file"], "b_says": pod.get("pod_remarks")})
    cod = ((history or {}).get("order") or {}).get("cod_amount") or 0
    m = COD_COLLECTED.search(pod.get("pod_remarks") or "")
    if m and abs(float(m.group(1)) - cod) >= 1:
        conflicts.append({"field": "cod", "a": f"orders ({sv}) #{oid}", "a_says": f"cod_amount {cod:.2f}",
                          "b": pod["source_file"], "b_says": f"COD collected {m.group(1)}"})
    elif cod and not m:
        conflicts.append({"field": "cod", "a": f"orders ({sv}) #{oid}", "a_says": f"cod_amount {cod:.2f}",
                          "b": pod["source_file"], "b_says": "no COD recorded on the scan"})
    data = {**pod, "received_by_role": bool(ROLE.search(pod.get("received_by") or ""))}
    return result(data, [pod["source_file"]], conflicts)


def get_gps_trail(ops, driver_code, delivered_time, town, version="v3"):
    """Pings for one driver within 30 min of the delivery, and the closest one to the delivery town."""
    if not (driver_code and delivered_time):
        return result(None, gaps=["no delivery time or driver to check GPS against"])
    if TOOL_VERSIONS[version]["gps_epoch_plus_8"]:
        # Bug: treats ts_epoch as true UTC and adds 8 h for Singapore. The GPS clock is 8 h slow (T16).
        centre_epoch = _epoch(delivered_time - dt.timedelta(hours=8))
    else:
        centre_epoch = _epoch(delivered_time - GPS_TO_SGT)
    w = int(GPS_WINDOW.total_seconds())
    pings = ops.all("SELECT ping_id, ts, lat, lon FROM gps_pings WHERE driver_code=? AND ts_epoch BETWEEN ? AND ? "
                    "ORDER BY ts_epoch", (driver_code, centre_epoch - w, centre_epoch + w))
    centre = TOWN_CENTROID.get(town)
    best = min(pings, key=lambda p: dist_m(p["lat"], p["lon"], *centre)) if pings and centre else None
    dmin = round(dist_m(best["lat"], best["lon"], *centre)) if best else None
    data = {"driver_code": driver_code, "epoch_window": [centre_epoch - w, centre_epoch + w], "pings": len(pings),
            "min_dist_m": dmin, "near_town": dmin is not None and dmin <= GPS_NEAR_M, "town": town}
    gaps = [] if pings else ["no GPS ping within 30 min of the delivery time (GPS is sparse: ~5 pings per driver per day)"]
    return result(data, [f"gps_pings#{best['ping_id']}"] if best else [], gaps=gaps)


def get_rate(ops, order, version="v3"):
    if order["order_date"] < RATE_CARD_START:
        return result(None, gaps=[f"rate card for {order['order_date']:%d %b %Y} not provided: the card starts 1 Apr 2026 (audit T24)"])
    if TOOL_VERSIONS[version]["zone_from_postal"]:
        zone, basis = zone_from_postal_sector(order.get("postal_code_clean")), "postal_sector"
    else:
        zone, basis = order.get("zone"), "town"
    if not zone:
        return result({"zone": None, "zone_basis": basis}, gaps=["no rate card zone for this address"])
    w = order.get("parcel_weight_kg") or 0
    row = ops.one("SELECT rate_id, zone, base_rate_sgd, source_file FROM rate_card WHERE zone=? AND weight_min_kg < ? "
                  "AND weight_max_kg >= ? AND effective_from <= ? ORDER BY effective_from DESC", (zone, w, w, order["order_date"]))
    if not row:
        return result({"zone": zone, "zone_basis": basis}, gaps=[f"no rate for {zone}, {w} kg"])
    gst = ops.one("SELECT rule_id FROM rate_card_rules WHERE rule_name='gst'")
    price = round(row["base_rate_sgd"] * (1 + GST), 2)
    srcs = [f"rate_card#{row['rate_id']} ({row['source_file']})"] + ([f"rate_card_rules#{gst['rule_id']}"] if gst else [])
    return result({"zone": zone, "zone_basis": basis, "base_rate_sgd": row["base_rate_sgd"], "expected_price": price}, srcs)


POLICY_TEXT = {
    "not_received": "Reject only if the delivery is confirmed by a POD scan or a GPS ping in the delivery town within "
                    "30 min; otherwise re-deliver (benefit of the doubt).",
    "late": "If deliveries.was_late = 1, compensate with a S$5.00 voucher; otherwise reject.",
    "damaged": "If the parcel was delivered, compensate S$15.00 goodwill; otherwise re-deliver.",
    "overcharged": "Refund the difference between the charge and the rate card price incl. 9% GST; no rate card for the "
                   "order date means the data cannot answer.",
}


def search_policy(category):
    text = POLICY_TEXT.get(category)
    return result({"rule": text, "needs": NEEDS.get(category, [])} if text else None,
                  [f"{POLICY_SOURCE} · {category}"] if text else [], gaps=[] if text else ["no policy rule for this complaint type"])
