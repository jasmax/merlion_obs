"""The triage pipeline as architecture.md draws it, instrumented span by span:

    intake (code)          extract order number + clues, mask PII, refuse out-of-scope input (no LLM)
    resolveOrder (code)    pick v1 or v2 before any LLM call; ambiguous -> ask the CS agent (no LLM)
    chat (LLM + tools)     ChatClient loop: the model picks getDeliveryHistory, getPodScan, getGpsTrail,
                           getRate, searchPolicy; every fact comes back with its source
    answer_check (code)    citations present, no personal data, money within the cap

Each step has a version (config["prompts"][step]); the chat step also has a model. In LLM_MODE=sim
(default) the model is simulated deterministically per (case, step, prompt hash, model), so an
unchanged step gives the same output and run diffs stay exact. Tool calls are always real queries
against merlion_clean. LLM_MODE=live/replay sends the chat step to OpenAI with real function calling.
"""
import datetime as dt
import hashlib
import json
import random
import re
from contextlib import contextmanager

from . import llm, tools as T
from .clock import CLOCK
from .pii import count_pii, mask
from .reference import MONEY_CAP_SGD, ORDER_ID_RANGE, RATE_CARD_START, SGT, VERDICTS, policy
from .registry import ref, text

COMPONENTS = ["intake", "resolveOrder", "tools", "chat", "answer_check"]
CODE_STEP = "-"  # model column for steps that are plain code


def config(resolve="v2", tools="v3", chat="v2", model="mini", max_tool_calls=8):
    return {"prompts": {"intake": "v1", "resolveOrder": resolve, "tools": tools, "chat": chat, "answer_check": "v1"},
            "models": {"intake": CODE_STEP, "resolveOrder": CODE_STEP, "tools": CODE_STEP, "chat": model, "answer_check": CODE_STEP},
            "max_tool_calls": max_tool_calls}


VERSIONS = {  # production releases
    "v0.1": config(resolve="v1", tools="v1", chat="v1"),   # naive first build
    "v0.2": config(),                                       # = configs/main.json
}
ORACLE = config()

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
ORDER_NO = re.compile(r"(?<![\d-])(\d{5})(?!\d)")
CORPORATE = re.compile(r"\bMERL-C-\d{3,5}\b", re.I)
PHONE = re.compile(r"(?<![\w-])(?:\+65[\s-]?)?([689]\d{3})[\s-]?(\d{4})(?!\w)")
CHARGED = re.compile(r"(?:charged|billed|paid|debited)\D{0,25}?S?\$\s?(\d+(?:\.\d{1,2})?)", re.I)
DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(m[:3] for m in MONTHS) + r")[a-z]*\b", re.I)
MONTH_ONLY = re.compile(r"\b(?:in|from|since|back in)\s+(" + "|".join(MONTHS) + r")\b", re.I)
SIGNOFF = re.compile(r"^(?:thanks|thank you|regards|best regards|best|cheers|sincerely)[,!.]?\s*$", re.I)
CATEGORY_RULES = [
    ("damaged", r"damag|broken|crushed|smashed|\btorn\b|dented|cracked|shattered"),
    ("overcharged", r"overcharg|\bcharged\b|\bbilled\b|price|refund the difference|\brate card\b|too much"),
    ("not_received", r"never (?:got|received|arrived|came)|not (?:been )?received|didn.?t (?:get|receive)|no sign of|"
                     r"nobody came|did not arrive|hasn.?t arrived|where is my parcel|missing"),
    ("late", r"\blate\b|delay|took (?:forever|ages|\d+ days)|days after|still not here|slow"),
]
TOOL_PLAN = {
    "not_received": ["getDeliveryHistory", "getPodScan", "getGpsTrail", "searchPolicy"],
    "late": ["getDeliveryHistory", "searchPolicy"],
    "damaged": ["getDeliveryHistory", "getPodScan", "searchPolicy"],
    "overcharged": ["getDeliveryHistory", "getRate", "searchPolicy"],
}
SIM_MODEL = {  # simulated behaviour per model alias: category error, wrong verdict, skips GPS, drops citations
    "mini": {"cat_err": 0.006, "verdict_err": 0.004, "skip_gps": 0.0, "no_cite": 0.0, "ms": 1.0},
    "nano": {"cat_err": 0.05, "verdict_err": 0.03, "skip_gps": 0.35, "no_cite": 0.2, "ms": 0.45},
}


def drng(*parts):
    return random.Random(int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


def wait(rng, ms, sd=0.3):
    CLOCK.advance(rng.lognormvariate(0, sd) * ms)


def classify(text_):
    t = text_.lower()
    for cat, pat in CATEGORY_RULES:
        if re.search(pat, t):
            return cat
    return "not_received"


def extract_clues(email):
    body, subject = email.get("body") or "", email.get("subject") or ""
    nums = {int(n) for n in ORDER_NO.findall(subject + "\n" + body) if ORDER_ID_RANGE[0] <= int(n) <= ORDER_ID_RANGE[1]}
    phone = PHONE.search(body)
    lines = [ln.strip() for ln in body.splitlines()]
    name = None
    for i, ln in enumerate(lines):
        if SIGNOFF.match(ln):
            nxt = next((x for x in lines[i + 1:] if x), "")
            if re.fullmatch(r"[A-Z][a-zA-Z'-]+(?: [A-Z][a-zA-Z'-]+){1,3}", nxt):
                name = nxt
    dm, mo = DAY_MONTH.search(body), MONTH_ONLY.search(body)
    hint = None
    if dm:
        hint = {"day": int(dm.group(1)), "month": [m[:3] for m in MONTHS].index(dm.group(2)[:3].lower()) + 1}
    elif mo:
        hint = {"day": None, "month": MONTHS.index(mo.group(1).lower()) + 1}
    ch = CHARGED.search(body)
    return {"order_id": next(iter(nums)) if len(nums) == 1 else None, "order_numbers_found": len(nums),
            "corporate_ref": bool(CORPORATE.search(subject + " " + body)),
            "phone": phone.group(1) + phone.group(2) if phone else None, "name": name, "date_hint": hint,
            "charged": float(ch.group(1)) if ch else None}


def build_facts(category, results, clues, order):
    """Facts for the policy, from the tool results the chat step actually obtained."""
    f = {"category": category, "order_found": True, "order": order}
    h = results.get("getDeliveryHistory")
    if h:
        last = h["data"]["last"]
        f["delivery_outcome"] = last["outcome"] if last else "NONE"
        f["was_late"] = bool(last["was_late"]) if last else None
        f["driver_code"] = last["driver_code"] if last else None
        f["zone"] = h["data"].get("zone")
    if category == "not_received":
        pod, gps = results.get("getPodScan"), results.get("getGpsTrail")
        if pod and pod["data"] and pod["data"].get("received_by"):
            f["evidence_of_delivery"] = "POD " + pod["data"]["source_file"]
        elif gps and gps["data"] and gps["data"]["near_town"]:
            f["evidence_of_delivery"] = "GPS " + gps["sources"][0]
        elif pod is not None and gps is not None:
            f["evidence_of_delivery"] = ""
    if category == "overcharged":
        r = results.get("getRate")
        f["expected_price"] = r["data"].get("expected_price") if r and r["data"] else None
        f["charged"] = clues.get("charged")
    return f


class _NullSpan:
    def set(self, **k): return self
    def out(self, v): return v
    def llm(self, *a): pass


class NullTracer:
    """Runs the pipeline without recording (oracle facts, fixture labels)."""
    @contextmanager
    def span(self, *a, **k):
        yield _NullSpan()

    def handoff(self, *a):
        pass


class Pipeline:
    def __init__(self, cfg, tracer, ops, incident=None, perfect=False, label=None):
        self.cfg, self.tr, self.ops = cfg, tracer, ops
        self.p, self.m = cfg["prompts"], cfg["models"]
        self.incident = incident or (lambda now: None)
        self.perfect = perfect          # oracle: no simulated model error
        self.label = label or "custom"

    def _rng(self, cid, step, *extra):
        return drng(cid, step, ref(step, self.p[step]), self.m.get(step, "-"), *extra)

    def _handoff(self, a, b, payload):
        if hasattr(self.tr, "handoff"):
            self.tr.handoff(a, b, payload)

    # ------------------------------------------------------------------ run
    def run(self, email, category=None):
        """category: force the complaint type (oracle only). Returns the outcome dict."""
        out = {"loop": False, "order": None, "facts": {}, "clues": None}
        with self.tr.span("triage", "agent", "pipeline") as root:
            root.set(**{"pipeline.release": self.label, "email.id": email["email_id"]})
            clues, masked = self.intake(email)
            out["clues"] = clues
            # Category for reporting when we stop before the LLM (a keyword guess; the LLM decides otherwise).
            out["facts"] = {"category": "corporate" if clues["corporate_ref"] and not clues["order_id"]
                            else category or classify(masked)}
            if clues["corporate_ref"] and not clues["order_id"]:
                return self._finish(root, out, {"outcome": "refuse", "amount": None, "citations": [],
                                                "reason": "corporate account reference: no table holds corporate accounts (audit T27)"})
            if not clues["order_id"]:
                return self._finish(root, out, {"outcome": "clarify", "amount": None, "citations": [],
                                                "reason": "no single order number in the email"})
            self._handoff("intake", "resolveOrder", {"order_id": clues["order_id"]})
            res = self.resolve(email, clues)
            if res["data"]["status"] != "RESOLVED":
                return self._finish(root, out, {"outcome": "clarify", "amount": None, "citations": res["sources"],
                                                "reason": res["data"]["reason"]})
            sv, oid = res["data"]["chosen"]
            out["order"] = {"system": sv, "no": oid}
            self._handoff("resolveOrder", "chat", out["order"])
            answer, facts, loop = self.chat(email, clues, masked, out["order"], category)
            out["facts"], out["loop"] = facts, loop
            self._handoff("chat", "answer_check", {"outcome": answer["outcome"]})
            answer = self.answer_check(email, answer, clues)
            return self._finish(root, out, answer)

    def _finish(self, root, out, answer):
        out["answer"] = answer
        out["policy"] = {"action": answer["outcome"], "amount": answer.get("amount")}
        f = out["facts"]
        out["driver_code"], out["zone"], out["category"] = f.get("driver_code"), f.get("zone"), f.get("category")
        root.out({"outcome": answer["outcome"], "amount": answer.get("amount")})
        return out

    # ------------------------------------------------------------------ steps
    def intake(self, email):
        rng = self._rng(email["email_id"], "intake")
        with self.tr.span("intake", "agent", "intake") as a:
            clues = extract_clues(email)
            masked, n = mask(f"Subject: {email.get('subject', '')}\n\n{email.get('body', '')}",
                             names=[clues["name"]] if clues["name"] else [])
            wait(rng, 4)
            a.set(**{"intake.order_numbers_found": clues["order_numbers_found"], "intake.pii_masked": n,
                     "intake.corporate_ref": clues["corporate_ref"], "intake.clues": [k for k in ("phone", "name", "date_hint", "charged") if clues[k]]})
            a.out({"order_id": clues["order_id"], "corporate_ref": clues["corporate_ref"],
                   "clues": sorted(k for k in ("phone", "name", "date_hint", "charged") if clues[k])})
            return clues, masked

    def resolve(self, email, clues):
        rng = self._rng(email["email_id"], "resolveOrder")
        received = email["received_at"].astimezone(SGT).replace(tzinfo=None)
        with self.tr.span("resolveOrder", "agent", "resolveOrder") as a:
            with self.tr.span("resolveOrder.query", "tool") as s:
                wait(rng, 35)
                res = T.resolve_order(self.ops, clues["order_id"], clues, received, self.p["resolveOrder"])
                d = res["data"]
                s.set(**{"order.query": clues["order_id"], "order.candidates": d["candidates"],
                         "order.candidate_systems": d["candidate_systems"], "order.status": d["status"],
                         "order.basis": d.get("basis"), "order.chosen": d.get("chosen"), "order.reason": d["reason"],
                         "order.scores": d.get("scores"), "tool.sources": res["sources"]})
                s.out({"status": d["status"], "chosen": d.get("chosen")})
            a.out({"status": d["status"], "chosen": d.get("chosen")})
            return res

    def _tool(self, name, order, clues, results, rng):
        tv = self.p["tools"]
        with self.tr.span("resolveOrder.recheck" if name == "resolveOrder" else name, "tool", "tools") as s:
            wait(rng, {"getDeliveryHistory": 45, "getPodScan": 30, "getGpsTrail": 120, "getRate": 25,
                       "searchPolicy": 60, "resolveOrder": 35}[name])
            sv, oid = order["system"], order["no"]
            h = results.get("getDeliveryHistory")
            if name == "getDeliveryHistory":
                r = T.get_delivery_history(self.ops, sv, oid)
                o = r["data"]["order"] or {}
                zone_basis = "postal_sector" if T.TOOL_VERSIONS[tv]["zone_from_postal"] else "town"
                r["data"]["zone"] = T.zone_from_postal_sector(o.get("postal_code_clean")) if zone_basis == "postal_sector" else o.get("zone")
                last = r["data"]["last"] or {}
                s.set(**{"delivery.attempts": len(r["data"]["attempts"]), "delivery.outcome": last.get("outcome"),
                         "delivery.time_sgt": last.get("delivered_time"), "delivery.was_late": last.get("was_late"),
                         "delivery.driver_code": last.get("driver_code"), "delivery.town": o.get("town"),
                         "delivery.zone": r["data"]["zone"], "delivery.zone_basis": zone_basis,
                         "delivery.postal_code": o.get("postal_code_clean")})
            elif name == "getPodScan":
                r = T.get_pod_scan(self.ops, sv, oid, h and h["data"])
                s.set(**{"pod.found": bool(r["data"]), "pod.source_file": r["data"] and r["data"]["source_file"],
                         "pod.received_by_role": r["data"] and r["data"]["received_by_role"],
                         "pod.conflicts": [c["field"] for c in r["conflicts"]]})
            elif name == "getGpsTrail":
                last = (h and h["data"]["last"]) or {}
                town = ((h and h["data"]["order"]) or {}).get("town")
                r = T.get_gps_trail(self.ops, last.get("driver_code"), last.get("delivered_time"), town, tv)
                if r["data"]:
                    s.set(**{"gps.driver_code": r["data"]["driver_code"], "gps.epoch_window": r["data"]["epoch_window"],
                             "gps.delivery_time_sgt": last.get("delivered_time"), "gps.pings_in_window": r["data"]["pings"],
                             "gps.min_dist_m": r["data"]["min_dist_m"], "gps.near_town": r["data"]["near_town"]})
            elif name == "getRate":
                o = (h and h["data"]["order"]) or self.ops.one("SELECT * FROM orders WHERE system_version=? AND order_id=?", (sv, oid))
                r = T.get_rate(self.ops, o, tv)
                if r["gaps"] and self.p["chat"] == "v1" and o["order_date"] < RATE_CARD_START:
                    # chat@v1 has no honest-gap rule: the model prices an old order off the Apr 2026 card anyway.
                    r = T.get_rate(self.ops, {**o, "order_date": RATE_CARD_START}, tv)
                    r["guessed"] = True
                s.set(**{"rate.zone": (r["data"] or {}).get("zone"), "rate.zone_basis": (r["data"] or {}).get("zone_basis"),
                         "rate.order_zone": o.get("zone"), "rate.expected_price": (r["data"] or {}).get("expected_price"),
                         "rate.order_date": o.get("order_date"), "rate.guessed": bool(r.get("guessed"))})
            elif name == "searchPolicy":
                r = T.search_policy(clues.get("_category"))
            else:  # resolveOrder called again from inside the chat loop
                r = T.resolve_order(self.ops, oid, clues, dt.datetime.combine(dt.date.today(), dt.time()), self.p["resolveOrder"])
            s.set(**{"tool.sources": r["sources"], "tool.gaps": r["gaps"], "tool.conflicts": len(r["conflicts"])})
            s.out({"sources": r["sources"], "gaps": r["gaps"], "conflicts": len(r["conflicts"])})
            results[name] = r
            return r

    def _llm_call(self, span_name, rng, tin, tout, ms, model, output=None):
        """One simulated model round trip, with retry under a provider incident."""
        for attempt in (1, 2):
            try:
                with self.tr.span(span_name, "llm") as s:
                    deg = self.incident(CLOCK.now())
                    wait(rng, ms * (deg["latency_x"] if deg else 1))
                    s.set(**{"retry.attempt": attempt, "llm.model": llm.model_id(model)})
                    if deg and rng.random() < deg["fail_rate"]:
                        raise TimeoutError("OpenAI API timed out (429/503)")
                    s.llm(model, ref("chat", self.p["chat"]), tin, tout)
                    if output is not None:
                        s.out(output)
                    return
            except TimeoutError:
                if attempt == 2:
                    raise

    def chat(self, email, clues, masked, order, category=None):
        cid, model, pv = email["email_id"], self.m["chat"], self.p["chat"]
        rng = self._rng(cid, "chat")
        beh = SIM_MODEL.get(model, SIM_MODEL["mini"])
        with self.tr.span("chat", "agent", "chat") as a:
            if llm.MODE != "sim" and not self.perfect:
                return self._chat_live(a, email, clues, masked, order)
            results, loop = {}, False
            try:
                cat = category or classify(masked)
                if not self.perfect and rng.random() < beh["cat_err"]:
                    cat = rng.choice([c for c in TOOL_PLAN if c != cat])
                clues["_category"] = cat
                tin = 1100 + len(masked) // 4
                plan = list(TOOL_PLAN[cat])
                if not self.perfect and "getGpsTrail" in plan and rng.random() < beh["skip_gps"]:
                    plan.remove("getGpsTrail")
                self._llm_call("chat.completion", rng, tin, 70, 900 * beh["ms"], model, {"category": cat, "tool_calls": plan})
                calls = 0
                for name in plan:
                    self._tool(name, order, clues, results, rng)
                    calls += 1
                    tin += 350
                if pv == "v3" and any(r["conflicts"] for r in results.values()):
                    # chat@v3: "when sources disagree, confirm the order again" -> the conflict never goes away.
                    while calls < self.cfg["max_tool_calls"]:
                        self._llm_call("chat.completion", rng, tin, 40, 600 * beh["ms"], model)
                        self._tool("resolveOrder", order, clues, results, rng)
                        calls += 1
                        tin += 250
                    loop = True
                if loop:
                    a.set(**{"chat.tool_calls": calls, "chat.loop_guard": True})
                    answer = {"outcome": "escalate", "amount": None, "citations": [], "conflicts": [],
                              "reason": f"tool-call limit ({self.cfg['max_tool_calls']}) reached"}
                    return a.out(answer), build_facts(cat, results, clues, order), True
                self._llm_call("chat.completion", rng, tin, 230, 1700 * beh["ms"], model)
            except TimeoutError:
                answer = {"outcome": "escalate", "amount": None, "citations": [], "conflicts": [],
                          "reason": "could not check: the LLM API is unavailable"}
                a.set(**{"chat.error": "llm_unavailable"})
                return a.out(answer), {"category": clues.get("_category"), "order_found": True}, False
            facts = build_facts(cat, results, clues, order)
            outcome, amount, reason = policy(facts)
            if not self.perfect and outcome in VERDICTS and rng.random() < beh["verdict_err"]:
                outcome = rng.choice([v for v in VERDICTS if v != outcome])
            cites = sorted({s for r in results.values() for s in r["sources"]})
            if not self.perfect and rng.random() < beh["no_cite"]:
                cites = []
            conflicts = [c for r in results.values() for c in r["conflicts"]] if pv != "v1" else []
            answer = {"category": cat, "outcome": outcome, "amount": amount, "reason": reason, "citations": cites,
                      "conflicts": conflicts, "gaps": [g for r in results.values() for g in r["gaps"]],
                      "guessed": [k for k, r in results.items() if r.get("guessed")]}
            a.set(**{"chat.tool_calls": len(results), "chat.tools": list(results), "chat.citations": len(cites),
                     "chat.conflicts_found": sum(len(r["conflicts"]) for r in results.values()),
                     "chat.conflicts_disclosed": len(conflicts), "chat.guessed": answer["guessed"]})
            a.out({"category": cat, "outcome": outcome, "amount": amount, "citations": len(cites), "conflicts": len(conflicts)})
            return answer, facts, False

    def _chat_live(self, a, email, clues, masked, order):
        """Real OpenAI function calling. The tools are bound to the verified order only."""
        schema = [{"type": "function", "function": {"name": n, "description": d, "parameters": {"type": "object", "properties": {}}}}
                  for n, d in [("getDeliveryHistory", "Every delivery attempt, driver, outcome and remark for the verified order"),
                               ("getPodScan", "The proof-of-delivery scan for the order (v1 orders only)"),
                               ("getGpsTrail", "Was the driver in the delivery town within 30 min of the delivery?"),
                               ("getRate", "What the customer should have paid, from the rate card")]]
        schema.append({"type": "function", "function": {"name": "searchPolicy", "description": "Policy rule for a complaint type",
                       "parameters": {"type": "object", "properties": {"category": {"type": "string", "enum": list(TOOL_PLAN)}},
                                      "required": ["category"]}}})
        msgs = [{"role": "system", "content": text("chat", self.p["chat"]) + "\n" + text("chat", "output")},
                {"role": "user", "content": f"Verified order: {order['system']} #{order['no']}\n\n{masked}"}]
        results, calls, rng = {}, 0, self._rng(email["email_id"], "chat")
        while True:
            with self.tr.span("chat.completion", "llm") as s:
                msg = llm.chat(s, self.m["chat"], msgs, schema, ref("chat", self.p["chat"]))
                s.out({"tool_calls": [t["name"] for t in msg["tool_calls"]]} if msg["tool_calls"] else {"answer": (msg["content"] or "")[:200]})
            if not msg["tool_calls"]:
                break
            msgs.append({"role": "assistant", "content": msg["content"], "tool_calls": [
                {"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": t["arguments"]}} for t in msg["tool_calls"]]})
            for t in msg["tool_calls"]:
                if t["name"] == "searchPolicy":
                    clues["_category"] = json.loads(t["arguments"] or "{}").get("category")
                r = self._tool(t["name"], order, clues, results, rng) if t["name"] in TOOL_PLAN["not_received"] + ["getRate"] \
                    else {"data": None, "sources": [], "conflicts": [], "gaps": ["unknown tool"]}
                payload, _ = mask(json.dumps({k: r[k] for k in ("data", "sources", "conflicts", "gaps")}, default=str))
                msgs.append({"role": "tool", "tool_call_id": t["id"], "content": payload[:6000]})
                calls += 1
            if calls >= self.cfg["max_tool_calls"]:
                a.set(**{"chat.loop_guard": True})
                return {"outcome": "escalate", "amount": None, "citations": [], "conflicts": [],
                        "reason": "tool-call limit reached"}, build_facts(clues.get("_category"), results, clues, order), True
        try:
            ans = json.loads(re.search(r"\{.*\}", msg["content"] or "", re.S).group(0))
        except (AttributeError, ValueError):
            ans = {"outcome": "escalate", "reason": "model answer was not valid JSON"}
        cat = ans.get("category") or clues.get("_category") or classify(masked)
        answer = {"category": cat, "outcome": ans.get("outcome", "escalate"), "amount": ans.get("amount"),
                  "reason": ans.get("reason", ""), "citations": ans.get("citations") or [], "conflicts": ans.get("conflicts") or [],
                  "gaps": [g for r in results.values() for g in r["gaps"]], "guessed": []}
        a.set(**{"chat.tool_calls": calls, "chat.tools": list(results), "chat.citations": len(answer["citations"]),
                 "chat.conflicts_found": sum(len(r["conflicts"]) for r in results.values()),
                 "chat.conflicts_disclosed": len(answer["conflicts"])})
        a.out({k: answer[k] for k in ("category", "outcome", "amount")})
        return answer, build_facts(cat, results, clues, order), False

    def answer_check(self, email, answer, clues):
        rng = self._rng(email["email_id"], "answer_check")
        with self.tr.span("answer_check", "agent", "answer_check") as a:
            wait(rng, 3)
            blob = json.dumps(answer, default=str)
            pii = count_pii(blob)
            problems = []
            if pii:
                problems.append("personal data in the answer")
            if answer["outcome"] in VERDICTS and not answer.get("citations"):
                problems.append("verdict without citations")
            if answer.get("amount") is not None and answer["amount"] > MONEY_CAP_SGD:
                problems.append(f"amount above the S${MONEY_CAP_SGD:.0f} cap")
            if problems:
                answer = {**answer, "outcome": "escalate", "amount": None,
                          "reason": "held for a human: " + "; ".join(problems)}
            answer["policy_note"] = "based on assumed policy"
            a.set(**{"check.pii_found": pii, "check.problems": problems, "check.citations": len(answer.get("citations") or [])})
            a.out({"outcome": answer["outcome"], "problems": problems})
            return answer


def oracle(ops, email, truth):
    """What a correct pipeline returns for the true order and complaint type (labels, component evals)."""
    p = Pipeline(ORACLE, NullTracer(), ops, perfect=True, label="oracle")
    saved = CLOCK._virtual
    try:
        if truth.get("category") in ("corporate", None) or truth.get("order_id") is None:
            return p.run(email)
        clues = extract_clues(email)
        results = {}
        order = {"system": truth["system_version"], "no": truth["order_id"]}
        clues["_category"] = truth["category"]
        for name in TOOL_PLAN[truth["category"]]:
            p._tool(name, order, clues, results, random.Random(0))
        facts = build_facts(truth["category"], results, clues, order)
        return {"facts": facts, "results": results}
    finally:
        CLOCK._virtual = saved


class TriageAgent:
    """Production wrapper: one release of the pipeline writing to the production trace store."""

    def __init__(self, version, tracer, ops, seed=0, incident=None, env="prod"):
        self.v, self.tracer, self.env = version, tracer, env
        self.pipe = Pipeline(VERSIONS[version], tracer, ops, incident=incident, label=version)

    def run(self, email):
        with self.tracer.trace(self.env, self.v, email["email_id"]) as t:
            out = self.pipe.run(email)
            o = out["order"] or {}
            t.out.update(category=out["category"], order_system=o.get("system"), order_no=o.get("no"),
                         resolution=out["answer"]["outcome"], amount=out["answer"].get("amount"),
                         driver_id=out["driver_code"], zone=out["zone"], reason=out["answer"].get("reason"))
        return t
