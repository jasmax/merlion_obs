"""Prompt registry. Every prompt is versioned and content-hashed; every model span
records `prompt.id@version#hash`, so a run diff can say exactly which prompt changed.

In a real repo these live as files under prompts/ (write_files() exports them) and
the hash is computed from the file, so an edit without a version bump still shows up.
"""
import hashlib
import os

PROMPTS = {
    "router": {
        "v1": "You triage Merlion delivery complaints. Classify the email as one of: not_received, late, "
              "damaged, overcharged, refund_followup. Extract the order number and any date mentioned. "
              "Hand off to order_resolver.",
        "v2": "You triage Merlion delivery complaints. Classify the email as one of: not_received, late, "
              "damaged, overcharged, refund_followup. Extract the order number and any date mentioned.\n"
              "Examples:\n- 'charged for express but booked standard' -> overcharged\n"
              "- 'still waiting for my refund from January' -> refund_followup\n"
              "- 'box crushed' -> damaged\nHand off to order_resolver.",
        "v3": "You triage Merlion delivery complaints. Classify the email, extract the order number and date. "
              "Before finalising, ask order_resolver to confirm the category matches the order record; "
              "if it disagrees, re-classify. Hand off to order_resolver.",
    },
    "order_resolver": {
        "v1": "Find the order with this number. Use lookup_v2 first, then lookup_v1.",
        "v2": "Find the order with this number. Order numbers repeat across the v1 (pre-Feb-2026) and v2 "
              "systems: call both. If both match, choose by (1) sender email == customer email, "
              "(2) the date in the email. If still tied, return AMBIGUOUS.",
    },
    "evidence": {
        "v1": "Collect delivery history, POD scan, GPS trail (+/-30 min of delivery) and rate card for the order.",
        "v2": "Collect delivery history, POD scan, GPS trail (+/-30 min of delivery) and rate card for the order. "
              "delivered_at is naive Asia/Singapore time; GPS ts_utc is UTC. Convert before comparing.",
        "v3": "Collect delivery history, POD scan, GPS trail (+/-30 min of delivery) and rate card for the order. "
              "delivered_at is naive Asia/Singapore time; GPS ts_utc is UTC. Convert before comparing. "
              "Validate postal codes (6 digits, sector 01-82); if invalid, geocode the street address.",
    },
    "policy": {
        "v1": "Apply the Merlion resolution policy to the evidence. Output one of redeliver, refund, "
              "compensate, reject, escalate, with an amount in SGD where relevant. Damage compensation "
              "is capped at the rate card's damage_cap.",
    },
    "writer": {
        "v1": "Write a short, polite reply to the customer explaining the resolution. No internal data.",
    },
}


def phash(agent, version):
    return hashlib.sha256(PROMPTS[agent][version].encode()).hexdigest()[:8]


def ref(agent, version):
    return f"{agent}@{version}#{phash(agent, version)}"


def write_files(root="prompts"):
    os.makedirs(root, exist_ok=True)
    for agent, vs in PROMPTS.items():
        for v, text in vs.items():
            with open(f"{root}/{agent}@{v}.md", "w") as f:
                f.write(text + "\n")
