"""Prompt and step registry. Every pipeline step is versioned and content-hashed, and every span
records `step@version#hash`, so a run diff says exactly which prompt or which code rule changed.

The chat entries are the Spring AI system prompt (backend/src/main/resources/prompts/system-prompt.st).
The code entries describe the rule each version of a code step implements (resolveOrder scoring,
the tool bugs, the answer check), so a change to them shows up in the diff the same way.
`write_files()` exports everything to prompts/<step>@<version>.md for review in pull requests.
"""
import hashlib
import os

PROMPTS = {
    "intake": {
        "v1": "Code. Extract one 5-digit order number (20001-34353; a MERL-C-xxxx corporate reference is not one), the "
              "phone number, the signature name, any date mentioned and any amount charged. Mask emails, phones, NRIC, "
              "addresses and names before any text reaches the LLM. Refuse corporate-account requests without an LLM call.",
    },
    "resolveOrder": {
        "v1": "Code. Look up the order number. On a v1/v2 collision take the newer system (v2).",
        "v2": "Code. Look up the order number in v1 and v2. Keep orders placed before the email. Score phone (+3), "
              "name (+2) and a date within 3 days of the delivery (+1). A unique top score wins; a tie returns "
              "AMBIGUOUS and the app asks the CS agent, without calling the LLM.",
    },
    "tools": {
        "v1": "Code. getGpsTrail converts ts_epoch to Singapore time with +8 h. getRate and the report zone use the "
              "postal sector.",
        "v2": "Code. getGpsTrail uses Singapore time = ts_epoch + 16 h (audit T16). The zone still comes from the "
              "postal sector.",
        "v3": "Code. getGpsTrail uses Singapore time = ts_epoch + 16 h (audit T16). The zone comes from orders.zone, "
              "i.e. the town in the address (audit T04).",
    },
    "chat": {
        "v1": "You are Merlion Express's complaint triage assistant. You get one masked complaint email and the "
              "verified order. Call the tools you need, then recommend re-deliver, refund, compensate or reject. "
              "Cite the source of every fact.",
        "v2": "You are Merlion Express's complaint triage assistant. You get one masked complaint email and the "
              "verified order. Call the tools you need, then recommend re-deliver, refund, compensate or reject.\n"
              "Rules:\n- Use only facts a tool returned, and cite the source of each one exactly as the tool gave it.\n"
              "- If a tool returns a gap, say \"the data does not show this\". Never fill it from memory; escalate "
              "when the verdict needs that fact.\n- When two sources disagree, show both with their sources. Never "
              "pick one silently.\n- Every verdict is \"based on assumed policy\".",
        "v3": "You are Merlion Express's complaint triage assistant. You get one masked complaint email and the "
              "verified order. Call the tools you need, then recommend re-deliver, refund, compensate or reject.\n"
              "Rules:\n- Use only facts a tool returned, and cite the source of each one exactly as the tool gave it.\n"
              "- If a tool returns a gap, say \"the data does not show this\". Never fill it from memory.\n"
              "- When two sources disagree, call resolveOrder again to confirm the order before answering.",
        "output": "Reply with JSON only: {\"category\": \"not_received|late|damaged|overcharged\", \"outcome\": "
                  "\"redeliver|refund|compensate|reject|escalate\", \"amount\": number or null, \"reason\": string, "
                  "\"citations\": [source strings], \"conflicts\": [string]}",
    },
    "answer_check": {
        "v1": "Code. Hold for a human any verdict without citations, any answer containing personal data, and any "
              "refund or compensation above S$30.",
    },
}


def phash(step, version):
    return hashlib.sha256(PROMPTS[step][version].encode()).hexdigest()[:8]


def ref(step, version):
    return f"{step}@{version}#{phash(step, version)}"


def text(step, version):
    return PROMPTS[step][version]


def write_files(root="prompts"):
    os.makedirs(root, exist_ok=True)
    for step, vs in PROMPTS.items():
        for v, t in vs.items():
            with open(f"{root}/{step}@{v}.md", "w", encoding="utf-8", newline="\n") as f:
                f.write(t + "\n")
