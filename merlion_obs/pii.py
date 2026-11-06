"""PII masking: the Python twin of the Spring masking component (backend/masking).

Used three times: on the email before any text reaches the LLM, on every span before it is
stored, and on the answer before it is returned (audit T28; PDPA).
Masks emails, SG phone numbers, NRIC/FIN, block/unit addresses, 6-digit postal codes, and any
names the caller already knows (the customer names on the candidate orders, the signature).
"""
import re

PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"(?<![\w#-])(?:\+65[\s-]?)?[689]\d{3}[\s-]?\d{4}(?![\w])"), "[PHONE]"),
    (re.compile(r"\b[STFGM]\d{7}[A-Z]\b"), "[NRIC]"),
    (re.compile(r"\b(?:Blk|Block)\s+\d+[A-Z]?\b[^,\n]*(?:,\s*#\d{1,3}-\d{1,4})?", re.I), "[ADDRESS]"),
    (re.compile(r"(?<![\w#-])\d{6}(?![\w])"), "[POSTAL]"),
]


def mask(text, names=()):
    """Return (masked_text, n_masked)."""
    n = 0
    for pat, repl in PATTERNS:
        text, k = pat.subn(repl, text)
        n += k
    for name in sorted({x for x in names if x and len(x) > 2}, key=len, reverse=True):
        text, k = re.subn(re.escape(name), "[NAME]", text, flags=re.I)
        n += k
    return text, n


def redact(value):
    """Mask every string inside a dict/list. Returns (value, n_masked)."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        out, n = {}, 0
        for k, v in value.items():
            out[k], m = redact(v)
            n += m
        return out, n
    if isinstance(value, (list, tuple)):
        out, n = [], 0
        for v in value:
            r, m = redact(v)
            out.append(r)
            n += m
        return out, n
    return value, 0


def count_pii(text):
    return sum(len(p.findall(text)) for p, _ in PATTERNS[:3])
