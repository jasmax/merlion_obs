import os
import re
import time

from guardrails import check_input, check_output, check_ownership
from llm import ask
from redact import redact_text
from data import DELIVERIES, ORDERS, POD_SCANS, RATE_CARD_FROM
from tracer import Tracer

tracer = Tracer()
ORDER_NO = re.compile(r"(?:order|#|ref)\D{0,15}?(\d{4,6})\b", re.I)    # "order no. 20078", "#104233"
BARE_NO = re.compile(r"(?<!postal code )(?<!postcode )(?<!\d)\d{5,6}(?!\d)", re.I)
CORPORATE = re.compile(r"\bMERL-C-\d{4}\b", re.I)                       # T27

# One model per step, set in .env. A cheap model for the easy step, a stronger one for the decision.
MODEL_CLASSIFY = os.environ.get("MODEL_CLASSIFY", "gpt-6-luna")
MODEL_RECOMMEND = os.environ.get("MODEL_RECOMMEND", "gpt-6.1-terra")

CATEGORIES = ["not_received", "late", "damaged", "overcharged", "refund_followup", "other"]
CHOICES = ["redeliver", "refund", "compensate", "reject", "investigate", "escalate"]
AMOUNTS = {"compensate": 5.00}                   # S$ set by policy in code, never by the model
REPLIES = {
    "redeliver": "We're sorry your parcel didn't arrive. We'll redeliver order {order_no} within 2 working days.",
    "refund": "We've started a refund for order {order_no}. It takes 5 to 7 working days to reach you.",
    "compensate": "We're sorry about order {order_no}. We've added an S${amount:.2f} voucher to your account.",
    "reject": "We checked order {order_no} and our delivery records show it was delivered as expected.",
    "investigate": "We're checking order {order_no} with our delivery team and will reply within 1 working day.",
    "escalate": "A customer service officer will review your message and reply within 1 working day.",
    "clarify": "We found more than one order {order_no} under your email. Please tell us the order date.",
}
SAFE_REPLY = REPLIES["escalate"]


def classify(email):
    with tracer.span("classify") as s:
        reply = ask(s, "Classify this parcel complaint. Reply with one label only: " + ", ".join(CATEGORIES),
                    email, MODEL_CLASSIFY)
        label = reply.strip().lower().rstrip(".")
        s["category"] = label if label in CATEGORIES else "other"
        return s["category"]


def find_order_no(text):
    m = ORDER_NO.search(text) or BARE_NO.search(text)
    return (m.group(1) if m.lastindex else m.group()) if m else None


def resolve_order(order_no, sender):
    """T01/T26: the same number can exist in v1 and v2. Pick the one this sender owns, in code."""
    with tracer.span("resolve_order", order_no=order_no) as s:
        time.sleep(0.05)                         # stands in for the database query
        candidates = [(sys, order_no) for sys in ("v1", "v2") if (sys, order_no) in ORDERS]
        owned = [k for k in candidates if check_ownership(ORDERS[k], sender)[0]]
        s.update(candidates=[f"{sys}:{no}" for sys, no in candidates], owned=len(owned))
        if not candidates:
            s["outcome"] = "not_found"
            return "not_found", None
        if not owned:
            s["outcome"] = "not_owner"
            return "not_owner", None
        if len(owned) > 1:
            s["outcome"] = "ambiguous"           # same customer, both systems: ask, don't guess
            return "ambiguous", None
        s["outcome"] = "found"
        s["system"] = owned[0][0]
        return "found", owned[0]


def gather_evidence(key):
    """Facts, sources and honest notes, built by code from the data. Never invented."""
    with tracer.span("gather_evidence") as s:
        order = ORDERS[key]
        sys, no = key
        facts = {"system": sys, "order_no": no, "status": order["status"], "order_date": order["order_date"]}
        sources, notes = [f"orders_{sys}#{no}"], []
        delivery = DELIVERIES.get(key)
        if delivery is None:
            notes.append(f"No delivery attempt on record (order status {order['status']}).")     # T07
        else:
            row, remark, at = delivery
            facts.update(delivered_at=at, delivery_remark=remark)
            sources.append(row)
        pod = POD_SCANS.get(key)
        if pod is None:
            notes.append("No POD scan on file.")                                                 # T21
        else:
            sources.append(pod["file"])
            facts["pod_received_by"] = pod["received_by"]
            if delivery and pod["remark"] != delivery[1]:                                        # T23
                notes.append(f"Sources disagree: the POD scan says '{pod['remark']}', "
                             f"the delivery record says '{delivery[1] or 'no remark'}'.")
            if pod["received_by"] != "customer":
                notes.append(f"Parcel was received by: {pod['received_by']}.")
        if order["order_date"] < RATE_CARD_FROM:
            notes.append("Rate card for this order date not provided.")                          # T24
        s.update(sources=sources, notes=notes)
        return facts, sources, notes


def recommend(facts, notes, email, category):
    with tracer.span("recommend") as s:
        reply = ask(s, "You triage parcel delivery complaints. Reply with exactly one word from this list: "
                    + ", ".join(CHOICES) + ".",
                    f"Category: {category}\nComplaint: {email}\nOrder facts: {facts}\nNotes: {notes}", MODEL_RECOMMEND)
        word = reply.strip().lower().rstrip(".")
        s["resolution"] = word if word in CHOICES else "escalate"   # never trust free text
        s["raw_reply"] = reply[:200]
        return s["resolution"]


def refuse(span, rule, message):
    span.update(resolution="refused", refused_by=rule)
    return {"resolution": "refused", "rule": rule, "reply": message, "notes": [], "sources": []}


def finish(span, resolution, order_no, notes, sources, amount=None):
    """Build the reply in code, run the output guard, return the result."""
    reply = REPLIES[resolution].format(order_no=order_no, amount=amount or 0)
    with tracer.span("output_guard") as g:
        problems = check_output(reply + " " + " ".join(notes), resolution, amount)
        g.update(blocked=bool(problems), problems=problems)
    if problems:
        resolution, amount, reply, notes = "escalate", None, SAFE_REPLY, ["Held for review by the output guard."]
    span.update(resolution=resolution, amount=amount, sources=sources)
    return {"resolution": resolution, "amount": amount, "reply": reply, "notes": notes, "sources": sources}


def triage(email, sender):
    with tracer.span("triage", email_chars=len(email), sender=sender) as s:
        # 1. Input guardrail on the raw text: no model call if it's refused.
        with tracer.span("input_guard") as g:
            ok, rule, message = check_input(email)
            g.update(blocked=not ok, rule=rule)
        if not ok:
            return refuse(s, rule, message)

        masked, found = redact_text(email)       # the model only ever sees the masked email
        s["pii_in_email"] = found
        if CORPORATE.search(masked):             # T27: no table holds corporate accounts
            return finish(s, "escalate", None, ["We can't look up corporate accounts here."], [])

        # 2. Resolve the order across v1/v2 and check ownership (the kill-shot), before any model call.
        order_no = find_order_no(masked)
        outcome, key = resolve_order(order_no, sender) if order_no else ("not_found", None)
        with tracer.span("ownership_guard") as g:
            g.update(blocked=outcome == "not_owner", rule="not_order_owner" if outcome == "not_owner" else None)
        if outcome == "not_owner":
            return refuse(s, "not_order_owner", "We couldn't match this order to your email address. "
                                                "Please write from the email you used when ordering.")
        if outcome == "not_found":
            return finish(s, "escalate", order_no, ["No matching order found."], [])
        if outcome == "ambiguous":               # T26 that the data can't settle: ask, without the model
            return finish(s, "clarify", order_no, ["Order number exists in both systems for this customer."], [])

        # 3. Evidence by code, decision by the model, reply and amount by code.
        facts, sources, notes = gather_evidence(key)
        resolution = recommend(facts, notes, masked, classify(masked))
        return finish(s, resolution, order_no, notes, sources, AMOUNTS.get(resolution))


if __name__ == "__main__":
    print(triage("Order 20078 says delivered but I never got it. Call me on 9123 4567.",
                 sender="wei.ling.tan@gmail.com"))
    print(triage("Order 20078 never arrived.", sender="ahmad.r@yahoo.com.sg"))
    print(triage("Where is my parcel? Order 20078.", sender="stranger@example.com"))