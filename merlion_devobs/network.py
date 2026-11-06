"""The triage agent network the dev team is building:

    router ─▶ order_resolver ─▶ evidence ─▶ policy ─▶ writer

Each agent is driven by a versioned prompt and a model from the run config.
Model outputs are simulated but *deterministic per (case, agent, prompt hash,
model)*, like temperature-0 calls: an agent whose prompt and model did not
change between two runs produces the same output, so a run diff isolates
exactly what a commit changed. Tool calls do real lookups in the ops DB.
"""
import datetime as dt
import hashlib
import random
import re
from contextlib import contextmanager

from merlion_obs.clock import CLOCK
from merlion_obs.data import SECTOR_ZONE, SGT, UTC, ZONES, _dist_m, card_for, policy
from .prompts import ref

CATS = ["not_received", "late", "damaged", "overcharged", "refund_followup"]
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
F = "%Y-%m-%dT%H:%M:%SZ"


def drng(*parts):
    return random.Random(int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


def wait(rng, ms, sd=0.3):
    CLOCK.advance(rng.lognormvariate(0, sd) * ms)


class _Null:
    """Tracer stand-in used to compute oracle facts without recording spans."""
    class S:
        def set(self, **k): return self
        def out(self, v): return v
        def llm(self, *a): pass
    @contextmanager
    def span(self, *a, **k):
        yield self.S()
    def handoff(self, *a): pass


# ---------------------------------------------------------------- evidence tools
def evidence_facts(tr, ops, order, cat, tz_aware, postal_fallback, rng):
    facts = {"category": cat, "order_found": True}
    if order["system"] == "v1":
        if cat == "refund_followup":
            with tr.span("get_v1_refund", "tool") as s:
                wait(rng, 50)
                r = ops.execute("SELECT amount FROM refunds_v1 WHERE job_no=? AND status='pending'", (order["no"],)).fetchone()
                facts["pending_refund"] = s.out(r[0] if r else None)
        return facts
    with tr.span("get_delivery_history", "tool") as s:
        wait(rng, 60)
        postal, address, svc, billed, fee, declared, created, promised, delivered, drv = ops.execute(
            "SELECT postal_code, address, service, billed_service, fee_billed, declared_value, created_at, "
            "promised_by, delivered_at, driver_id FROM orders_v2 WHERE order_no=?", (order["no"],)).fetchone()
        d_utc = dt.datetime.fromisoformat(delivered).replace(tzinfo=SGT if tz_aware else UTC).astimezone(UTC)
        valid = len(postal) == 6 and postal.isdigit() and int(postal[:2]) in SECTOR_ZONE
        if valid or not postal_fallback:
            g = ops.execute("SELECT lat, lng FROM geo_postal WHERE postal_code=?", (postal,)).fetchone()
            coords, geo = (g, "postal") if g else (ZONES[SECTOR_ZONE.get(int(postal[:2]), "Central")][:2], "sector_centroid")
        else:
            wait(rng, 250)
            coords, geo = ops.execute("SELECT lat, lng FROM geo_address WHERE address=?", (address,)).fetchone(), "address_fallback"
        late = (dt.datetime.fromisoformat(delivered) - dt.datetime.fromisoformat(promised)).total_seconds() / 3600
        s.set(**{"delivery.raw_time": delivered, "delivery.tz_assumed": "Asia/Singapore" if tz_aware else "UTC",
                 "delivery.postal_code": postal, "delivery.postal_valid": valid, "delivery.geocode": geo})
        facts.update(late_hours=round(late, 2), service=svc, billed_service=billed, fee_billed=fee, declared_value=declared)
        s.out({k: facts[k] for k in ("late_hours", "service", "billed_service", "fee_billed", "declared_value")} | {"geocode": geo})
    if cat in ("not_received", "damaged"):
        with tr.span("get_pod_scan", "tool") as s:
            wait(rng, 50)
            r = ops.execute("SELECT scan_id, damage_visible FROM pod_v2 WHERE order_no=?", (order["no"],)).fetchone()
            facts["has_pod"], facts["damage_visible"] = bool(r), bool(r and r[1])
            s.out({"has_pod": facts["has_pod"], "damage_visible": facts["damage_visible"]})
    if cat == "not_received":
        with tr.span("get_gps_trail", "tool") as s:
            wait(rng, 150)
            t0, t1 = d_utc - dt.timedelta(minutes=30), d_utc + dt.timedelta(minutes=30)
            pings = ops.execute("SELECT lat, lng FROM gps_pings WHERE driver_id=? AND ts_utc BETWEEN ? AND ?",
                                (drv, t0.strftime(F), t1.strftime(F))).fetchall()
            dmin = min((_dist_m(*p, *coords) for p in pings), default=None)
            facts["gps_at_address"] = dmin is not None and dmin <= 100
            s.set(**{"gps.window_start_utc": t0.strftime(F), "gps.pings_in_window": len(pings),
                     "gps.min_dist_m": round(dmin) if dmin is not None else None})
            s.out({"gps_at_address": facts["gps_at_address"]})
    if cat in ("late", "overcharged", "damaged"):
        with tr.span("check_rate_card", "tool") as s:
            wait(rng, 900)
            facts["card"] = card_for(created[:10])
            s.out({"effective_from": facts["card"]["effective_from"]})
    return facts


# ---------------------------------------------------------------- the network
class Network:
    def __init__(self, cfg, tracer, ops):
        self.cfg, self.tr, self.ops = cfg, tracer, ops
        self.p, self.m = cfg["prompts"], cfg["models"]

    def _agent_rng(self, case_id, agent):
        return drng(case_id, agent, ref(agent, self.p[agent]), self.m.get(agent, "-"))

    def run(self, email):
        cid, tr = email["email_id"], self.tr
        out = {"loop": False}
        with tr.span("triage_network", "agent", "network") as root:
            r = self.router(email)
            out["router"] = r
            tr.handoff("router", "order_resolver", {"order_no": r["order_no"], "category": r["category"]})
            order = self.resolver(email, r)
            # router@v3 asks the resolver to confirm the category; overcharged emails bounce forever.
            if self.p["router"] == "v3" and r["category"] == "overcharged" and order:
                while tr.ctx["hops"] < self.cfg["max_hops"]:
                    tr.handoff("order_resolver", "router", {"reason": "category disagrees with order record (service tier)"})
                    r = self.router(email, retry=tr.ctx["hops"])
                    tr.handoff("router", "order_resolver", {"order_no": r["order_no"], "category": r["category"]})
                out["loop"] = True
                order = None
            out["order"] = order and {"system": order["system"], "no": order["no"]}
            facts = {"category": r["category"], "order_found": False}
            if order:
                tr.handoff("order_resolver", "evidence", out["order"])
                facts = self.evidence(email, r, order)
            out["facts"] = facts
            tr.handoff("evidence" if order else "order_resolver", "policy", {"facts": "…"})
            action, amount = self.policy(email, facts)
            out["policy"] = {"action": action, "amount": amount}
            tr.handoff("policy", "writer", out["policy"])
            self.writer(email, action, amount)
            root.out(out["policy"])
        return out

    def router(self, email, retry=0):
        rng = self._agent_rng(email["email_id"], "router")
        with self.tr.span("router", "agent", "router") as a:
            with self.tr.span("router.classify", "llm") as s:
                body = email["body"]
                m = re.search(r"\b(1\d{5})\b", body)
                cat = email["truth"]["category"]
                err = {"v1": 0.06, "v2": 0.015, "v3": 0.06}[self.p["router"]]
                if rng.random() < err:
                    cat = rng.choice([c for c in CATS if c != cat])
                d = re.search(r"\b(\d{1,2}) ([A-Z][a-z]{2})\b", body)
                mo = re.search(r"from (January|February|March|April|May|June|July|August|September|October|November|December)", body)
                hint = {"day": int(d.group(1)), "month": MONTHS[d.group(2).lower()]} if d else (
                    {"day": None, "month": MONTHS[mo.group(1)[:3].lower()]} if mo else None)
                wait(drng(email["email_id"], "router", retry), 850)
                s.llm(self.m["router"], ref("router", self.p["router"]), 900 + len(body) // 4 + 120 * (self.p["router"] == "v2"), 80)
                res = s.out({"category": cat, "order_no": int(m.group(1)) if m else None, "date_hint": hint})
            return a.out(res)

    def resolver(self, email, r):
        rng = self._agent_rng(email["email_id"], "order_resolver")
        v = self.p["order_resolver"]
        with self.tr.span("order_resolver", "agent", "order_resolver") as a:
            cands = []
            with self.tr.span("lookup_v2", "tool") as s:
                wait(rng, 40)
                for no, em, d, drv in self.ops.execute("SELECT order_no, customer_email, delivered_at, driver_id FROM orders_v2 WHERE order_no=?", (r["order_no"],)):
                    cands.append({"system": "v2", "no": no, "email": em, "date": dt.datetime.fromisoformat(d), "driver_id": drv})
                s.out(len(cands))
            if v == "v2" or not cands:
                with self.tr.span("lookup_v1", "tool") as s:
                    wait(rng, 40)
                    n0 = len(cands)
                    for no, em, d, drv in self.ops.execute("SELECT job_no, cust_email, done_time, drv FROM jobs_v1 WHERE job_no=?", (r["order_no"],)):
                        cands.append({"system": "v1", "no": no, "email": em, "date": dt.datetime.strptime(d, "%d/%m/%Y %H:%M"), "driver_id": drv})
                    s.out(len(cands) - n0)
            with self.tr.span("order_resolver.choose", "llm") as s:
                wait(rng, 700)
                s.llm(self.m["order_resolver"], ref("order_resolver", v), 700 + 150 * len(cands), 60)
                chosen, basis = None, "not_found"
                if len(cands) == 1 or (cands and v == "v1"):
                    chosen, basis = cands[0], "single_match" if len(cands) == 1 else "first_match"
                elif cands:
                    h = r["date_hint"] or {}
                    def score(c):
                        sc = 2 if c["email"].lower() == email["from_email"].lower() else 0
                        if h.get("month") == c["date"].month:
                            sc += 1 + (h.get("day") == c["date"].day)
                        return sc
                    sc = sorted(((score(c), i) for i, c in enumerate(cands)), reverse=True)
                    if sc[0][0] > 0 and sc[0][0] != sc[1][0]:
                        chosen, basis = cands[sc[0][1]], "email+date"
                    else:
                        basis = "ambiguous"
                s.set(**{"order.candidates": len(cands), "order.basis": basis})
                s.out({"basis": basis, "chosen": chosen and f"{chosen['system']}:{chosen['no']}"})
            return a.out(chosen and {k: chosen[k] for k in ("system", "no", "driver_id")}) or None

    def evidence(self, email, r, order):
        ev = self.p["evidence"]
        rng = self._agent_rng(email["email_id"], "evidence")
        with self.tr.span("evidence", "agent", "evidence") as a:
            facts = evidence_facts(self.tr, self.ops, order, r["category"], ev in ("v2", "v3"), ev == "v3", rng)
            with self.tr.span("evidence.summarise", "llm") as s:
                wait(rng, 600)
                s.llm(self.m["evidence"], ref("evidence", ev), 1400, 140)
            a.out({k: v for k, v in facts.items() if k != "card"})
            return facts

    def policy(self, email, facts):
        model = self.m["policy"]
        rng = self._agent_rng(email["email_id"], "policy")
        with self.tr.span("policy", "agent", "policy") as a:
            with self.tr.span("policy.decide", "llm") as s:
                action, amount = policy(facts)
                if model == "haiku" and action == "compensate" and facts.get("category") == "damaged":
                    amount = facts["declared_value"]  # small model drops the damage cap
                if action != "escalate" and rng.random() < (0.035 if model == "haiku" else 0.012):
                    action = rng.choice([x for x in ["redeliver", "refund", "compensate", "reject"] if x != action])
                wait(rng, 1700 if model == "sonnet" else 650)
                s.llm(model, ref("policy", self.p["policy"]), 2100, 160)
                s.set(**{"input.facts": {k: v for k, v in facts.items() if k != "card"}})
                s.out({"action": action, "amount": amount})
            return a.out((action, amount))

    def writer(self, email, action, amount):
        rng = self._agent_rng(email["email_id"], "writer")
        with self.tr.span("writer", "agent", "writer") as a:
            with self.tr.span("writer.draft", "llm") as s:
                wait(rng, 1500)
                s.llm(self.m["writer"], ref("writer", self.p["writer"]), 1100, 240)
                s.set(**{"output.to": email["from_email"]})
                a.out(s.out(f"We will {action}" + (f" S${amount:.2f}" if amount else "")))


def oracle_facts(ops, email):
    """Facts a correct evidence agent would return for the *true* order."""
    t = email["truth"]
    order = {"system": t["system"], "no": t["order_no"]}
    if t["system"] == "v2":
        order["driver_id"] = ops.execute("SELECT driver_id FROM orders_v2 WHERE order_no=?", (t["order_no"],)).fetchone()[0]
    saved = CLOCK._virtual
    try:
        return evidence_facts(_Null(), ops, order, t["category"], True, True, random.Random(0))
    finally:
        CLOCK._virtual = saved
