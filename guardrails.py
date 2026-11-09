"""Guardrails that live in code, not in the prompt.

check_input      runs on the raw email BEFORE any model call. Refuses deterministically.
check_ownership  the kill-shot: only the customer who owns an order may see or act on it.
check_output     runs on the final reply BEFORE the customer sees it.
"""
import re

from redact import redact_text

COMPENSATION_CAP = 100.00          # S$; anything above needs a human
ALLOWED = {"redeliver", "refund", "compensate", "reject", "investigate", "escalate", "clarify"}

# (rule name, pattern, message to the customer). First match wins.
INPUT_RULES = [
    ("prompt_injection",
     re.compile(r"ignore (all |any )?(previous|prior|above|your) (instructions|rules)|system prompt|"
                r"developer mode|you are now|pretend (to be|you are)|jailbreak", re.I),
     "We can only help with delivery complaints."),
    ("third_party_data",
     re.compile(r"\bdriver'?s\s+(phone|mobile|number|contact|address|home|ic|nric|full name)\b|"         # driver's phone
                r"\b(phone|mobile|number|contact|address|ic|nric)\s+(of|for)\s+(the|your|that)?\s*driver\b|"  # number of the driver
                r"\bwhere\s+(does|do)\s+(the|your|that)\s+driver\s+(live|stay)\b|"                       # where does the driver live
                r"\b(other|another)\s+customer'?s?\b|\bsomeone\s+else'?s\s+(order|parcel|address)\b", re.I),
     "We can't share information about our staff or other customers."),
]
IN_SCOPE = re.compile(r"\b(order|parcel|package|deliver\w*|courier|refund\w*|charg\w*|damag\w*|late|"
                      r"missing|tracking|shipment|redeliver\w*|pod|proof of delivery)\b", re.I)


def check_input(text):
    """Return (ok, rule, message). No model is called."""
    for rule, pattern, message in INPUT_RULES:
        if pattern.search(text):
            return False, rule, message
    if not IN_SCOPE.search(text):
        return False, "out_of_scope", "We can only help with delivery complaints. Please include your order number."
    return True, None, None


def check_ownership(order, sender):
    """The kill-shot. A customer quoting someone else's order number must get nothing about that order:
    no status, no address, no photo, no refund. Compared in code, never by the model."""
    if order is None:
        return True, None, None
    if (sender or "").strip().lower() != order["customer_email"].lower():
        return False, "not_order_owner", ("We couldn't match this order to your email address. "
                                          "Please write from the email you used when ordering.")
    return True, None, None


def check_output(reply, resolution, amount=None):
    """Return a list of problems with the reply. Empty list = safe to send."""
    problems = []
    _, pii = redact_text(reply)
    if pii:
        problems.append(f"personal data in reply: {sorted(pii)}")
    if resolution not in ALLOWED:
        problems.append(f"resolution not allowed: {resolution!r}")
    if amount is not None and amount > COMPENSATION_CAP:
        problems.append(f"amount S${amount:.2f} over the S${COMPENSATION_CAP:.0f} cap")
    if re.search(r"\b(as an ai|language model|system prompt)\b", reply, re.I):
        problems.append("reply talks about the AI instead of the complaint")
    return problems