"""Instrumented complaint-triage agent (simulated LLM, real tool logic).

The LLM calls are simulated (deterministic, with token counts and a small error
rate) so the stack runs offline; the tool calls do real lookups against the ops
DB. To instrument your real agent, keep the `with tracer.span(...)` blocks and
replace the bodies.

Versions:
  v0.1 — the naive first build: takes the v2 order on a number collision, reads
         naive timestamps as UTC, trusts postal codes.
  v0.2 — disambiguates by customer email + date, reads naive times as SGT,
         validates postal codes and falls back to address geocoding.
"""
import datetime as dt
import random
import re

from .clock import CLOCK
from .data import SECTOR_ZONE, ZONES, SGT, UTC, _dist_m, card_for, policy

VERSIONS = {
    "v0.1": dict(disambiguate=False, tz_aware=False, validate_postal=False),
    "v0.2": dict(disambiguate=True, tz_aware=True, validate_postal=True),
}
MODEL = "claude-sonnet-5-5 (simulated)"
CATS = ["not_received", "late", "damaged", "overcharged", "refund_followup"]
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _lat(rng, ms, sd=0.35):
    CLOCK.advance(rng.lognormvariate(0, sd) * ms)


class TriageAgent:
    def __init__(self, version, tracer, ops, seed=0, incident=None, env="prod"):
        self.v, self.cfg = version, VERSIONS[version]
        self.tracer, self.ops, self.env = tracer, ops, env
        self.rng = random.Random(seed)
        self.incident = incident or (lambda now: None)

    # ------------------------------------------------------------------ run
    def run(self, email):
        with self.tracer.trace(self.env, self.v, email["email_id"]) as t:
            parsed = self.parse_email(email)
            facts = {"category": parsed["category"], "order_found": False}
            order = self.resolve_order(parsed, email)
            if order:
                facts["order_found"] = True
                t.out.update(order_system=order["system"], order_no=order["no"],
                             driver_id=order["driver_id"])
                hist = self.delivery_history(order)
                t.out["zone"] = hist["zone"]
                facts.update(hist["facts"])
                cat = parsed["category"]
                try:
                    if cat in ("not_received", "damaged") and order["system"] == "v2":
                        facts.update(self.get_pod(order, ocr_damage=(cat == "damaged")))
                    if cat == "not_received" and order["system"] == "v2":
                        facts.update(self.gps_trail(order, hist))
                    if cat in ("late", "overcharged", "damaged") and order["system"] == "v2":
                        facts["card"] = self.rate_card(order)
                    if cat == "refund_followup":
                        facts["pending_refund"] = self.v1_refund(order)
                except TimeoutError:
                    facts["order_found"] = False  # missing evidence -> escalate to human
                    facts["evidence_missing"] = True
            action, amount = self.recommend(facts)
            self.draft_reply(email, action, amount)
            t.out.update(category=parsed["category"], resolution=action, amount=amount)
        return t

    # ---------------------------------------------------------------- steps
    def parse_email(self, email):
        with self.tracer.span("parse_email", "llm") as s:
            body = email["body"]
            m = re.search(r"\b(1\d{5})\b", body)
            cat = email["truth"]["category"]
            if self.rng.random() < 0.03:  # classifier error rate
                cat = self.rng.choice([c for c in CATS if c != cat])
            date = re.search(r"\b(\d{1,2}) ([A-Z][a-z]{2})\b", body)
            month = re.search(r"from (January|February|March|April|May|June|July|August|September|October|November|December)", body)
            hint = None
            if date:
                hint = {"day": int(date.group(1)), "month": MONTHS[date.group(2).lower()]}
            elif month:
                hint = {"day": None, "month": MONTHS[month.group(1)[:3].lower()]}
            _lat(self.rng, 900)
            s.llm_usage(MODEL, 850 + len(body) // 4, 70)
            s.set(**{"input.email": body, "output.order_no": m.group(1) if m else None,
                     "output.category": cat, "output.date_hint": hint})
            return {"order_no": int(m.group(1)) if m else None, "category": cat, "date_hint": hint}

    def resolve_order(self, parsed, email):
        with self.tracer.span("resolve_order", "tool") as s:
            no = parsed["order_no"]
            cands = []
            for r in self.ops.execute("SELECT order_no, customer_email, delivered_at, driver_id FROM orders_v2 WHERE order_no=?", (no,)):
                cands.append({"system": "v2", "no": r[0], "email": r[1],
                              "date": dt.datetime.fromisoformat(r[2]), "driver_id": r[3]})
            for r in self.ops.execute("SELECT job_no, cust_email, done_time, drv FROM jobs_v1 WHERE job_no=?", (no,)):
                cands.append({"system": "v1", "no": r[0], "email": r[1],
                              "date": dt.datetime.strptime(r[2], "%d/%m/%Y %H:%M"), "driver_id": r[3]})
            _lat(self.rng, 40)
            s.set(**{"order.query": no, "order.candidates": len(cands),
                     "order.candidate_systems": [c["system"] for c in cands]})
            if not cands:
                s.set(**{"order.basis": "not_found"})
                return None
            if not self.cfg["disambiguate"] or len(cands) == 1:
                chosen = cands[0]
                basis = "single_match" if len(cands) == 1 else "first_match"
            else:
                hint = parsed["date_hint"] or {}
                def score(c):
                    sc = 2 if c["email"].lower() == email["from_email"].lower() else 0
                    if hint.get("month") == c["date"].month:
                        sc += 1
                        if hint.get("day") == c["date"].day:
                            sc += 1
                    return sc
                scored = sorted(((score(c), c) for c in cands), key=lambda x: -x[0])
                s.set(**{"order.scores": {c["system"]: sc for sc, c in scored}})
                if scored[0][0] == 0 or scored[0][0] == scored[1][0]:
                    s.set(**{"order.basis": "ambiguous"})
                    return None
                chosen, basis = scored[0][1], "email+date"
            s.set(**{"order.chosen_system": chosen["system"], "order.basis": basis})
            return chosen

    def delivery_history(self, o):
        with self.tracer.span("get_delivery_history", "tool") as s:
            _lat(self.rng, 60)
            if o["system"] == "v1":
                r = self.ops.execute("SELECT postcode, addr, done_time FROM jobs_v1 WHERE job_no=?", (o["no"],)).fetchone()
                s.set(**{"delivery.raw_time": r[2], "delivery.postal_code": r[0]})
                return {"zone": SECTOR_ZONE.get(int(r[0][:2]), "UNKNOWN"), "facts": {}, "coords": None}
            r = self.ops.execute(
                "SELECT postal_code, address, service, billed_service, fee_billed, declared_value, "
                "created_at, promised_by, delivered_at FROM orders_v2 WHERE order_no=?", (o["no"],)).fetchone()
            postal, address, svc, billed, fee, declared, created, promised, delivered = r
            local_tz = SGT if self.cfg["tz_aware"] else UTC
            d_utc = dt.datetime.fromisoformat(delivered).replace(tzinfo=local_tz).astimezone(UTC)
            late_h = (dt.datetime.fromisoformat(delivered) - dt.datetime.fromisoformat(promised)).total_seconds() / 3600
            valid = len(postal) == 6 and postal.isdigit() and int(postal[:2]) in SECTOR_ZONE
            coords, geo = None, "postal"
            if valid or not self.cfg["validate_postal"]:
                g = self.ops.execute("SELECT lat, lng FROM geo_postal WHERE postal_code=?", (postal,)).fetchone()
                if g:
                    coords = g
                else:  # naive fallback: centroid of whatever sector the first 2 digits say
                    z = SECTOR_ZONE.get(int(postal[:2]), "Central")
                    coords, geo = ZONES[z][:2], "sector_centroid"
                zone = SECTOR_ZONE.get(int(postal[:2]), "UNKNOWN")
            if self.cfg["validate_postal"] and not valid:
                _lat(self.rng, 250)
                g = self.ops.execute("SELECT lat, lng FROM geo_address WHERE address=?", (address,)).fetchone()
                coords, geo = g, "address_fallback"
                zone = min(ZONES, key=lambda z: _dist_m(*ZONES[z][:2], *g))
            s.set(**{"delivery.raw_time": delivered, "delivery.tz_assumed": "Asia/Singapore" if self.cfg["tz_aware"] else "UTC",
                     "delivery.time_utc": d_utc.isoformat(), "delivery.postal_code": postal,
                     "delivery.postal_valid": valid, "delivery.geocode": geo, "delivery.zone": zone,
                     "delivery.late_hours": round(late_h, 1)})
            return {"zone": zone, "coords": coords, "time_utc": d_utc,
                    "facts": {"late_hours": late_h, "service": svc, "billed_service": billed,
                              "fee_billed": fee, "declared_value": declared}}

    def get_pod(self, o, ocr_damage):
        with self.tracer.span("get_pod_scan", "llm" if ocr_damage else "tool") as s:
            r = self.ops.execute("SELECT scan_id, damage_visible FROM pod_v2 WHERE order_no=?", (o["no"],)).fetchone()
            _lat(self.rng, 1600 if ocr_damage and r else 50)
            if ocr_damage and r:
                s.llm_usage(MODEL, 1700, 60)  # vision call on the POD image
            s.set(**{"pod.found": bool(r), "pod.scan_id": r[0] if r else None,
                     "pod.damage_visible": bool(r[1]) if r else None})
            return {"has_pod": bool(r), "damage_visible": bool(r and r[1])}

    def gps_trail(self, o, hist):
        with self.tracer.span("get_gps_trail", "tool") as s:
            t0 = hist["time_utc"] - dt.timedelta(minutes=30)
            t1 = hist["time_utc"] + dt.timedelta(minutes=30)
            f = "%Y-%m-%dT%H:%M:%SZ"
            pings = self.ops.execute("SELECT lat, lng FROM gps_pings WHERE driver_id=? AND ts_utc BETWEEN ? AND ?",
                                     (o["driver_id"], t0.strftime(f), t1.strftime(f))).fetchall()
            dmin = min((_dist_m(*p, *hist["coords"]) for p in pings), default=None)
            _lat(self.rng, 150)
            s.set(**{"gps.driver_id": o["driver_id"], "gps.window_start_utc": t0.strftime(f),
                     "gps.window_end_utc": t1.strftime(f), "gps.pings_in_window": len(pings),
                     "gps.min_dist_m": round(dmin) if dmin is not None else None})
            return {"gps_at_address": dmin is not None and dmin <= 100}

    def rate_card(self, o):
        for attempt in (1, 2):
            try:
                with self.tracer.span("check_rate_card", "llm") as s:
                    deg = self.incident(CLOCK.now())
                    _lat(self.rng, 1800 * (deg["latency_x"] if deg else 1))
                    s.set(**{"retry.attempt": attempt})
                    if deg and self.rng.random() < deg["fail_rate"]:
                        raise TimeoutError("rate-card OCR timed out")
                    s.llm_usage(MODEL, 1400, 120)
                    created = self.ops.execute("SELECT created_at FROM orders_v2 WHERE order_no=?", (o["no"],)).fetchone()[0]
                    card = card_for(created[:10])
                    s.set(**{"rate_card.effective_from": card["effective_from"]})
                    return card
            except TimeoutError:
                if attempt == 2:
                    raise

    def v1_refund(self, o):
        with self.tracer.span("get_v1_refund", "tool") as s:
            _lat(self.rng, 50)
            r = None
            if o["system"] == "v1":
                r = self.ops.execute("SELECT amount FROM refunds_v1 WHERE job_no=? AND status='pending'", (o["no"],)).fetchone()
            s.set(**{"refund.system_queried": o["system"], "refund.pending_amount": r[0] if r else None})
            return r[0] if r else None

    def recommend(self, facts):
        with self.tracer.span("recommend_resolution", "llm") as s:
            action, amount = policy(facts)
            if action != "escalate" and self.rng.random() < 0.03:  # residual model error
                action = self.rng.choice([a for a in ["redeliver", "refund", "compensate", "reject"] if a != action])
            _lat(self.rng, 2200)
            s.llm_usage(MODEL, 2300, 180)
            s.set(**{"input.facts": {k: v for k, v in facts.items() if k != "card"},
                     "output.resolution": action, "output.amount": amount})
            return action, amount

    def draft_reply(self, email, action, amount):
        with self.tracer.span("draft_reply", "llm") as s:
            _lat(self.rng, 1900)
            s.llm_usage(MODEL, 1200, 260)
            s.set(**{"output.to": email["from_email"],
                     "output.text": f"Dear customer, regarding your complaint we will {action}"
                                    + (f" S${amount:.2f}" if amount else "") + "."})
