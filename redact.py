"""Remove personal data from text before it is stored or sent to a model.

Covers what a regex can find reliably: email addresses, Singapore phone numbers, NRIC/FIN numbers,
payment card numbers and unit numbers. It does not find names; that needs a classifier (later step).
Order numbers (6 digits), postal codes, dates and amounts are left alone: the app needs them.
"""
import re


def _luhn_ok(digits):
    """Payment cards pass the Luhn checksum; random long numbers usually don't."""
    total, double = 0, False
    for d in reversed(digits):
        n = int(d) * (2 if double else 1)
        total += n - 9 if n > 9 else n
        double = not double
    return total % 10 == 0


def _card(m):
    digits = re.sub(r"\D", "", m.group())
    return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group()


PATTERNS = [  # order matters: cards before phones, so a card isn't half-matched as a phone
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    ("card", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), _card),
    ("nric", re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE), "[NRIC]"),
    ("phone", re.compile(r"(?<!\d)(?<!\d[\s-])(?:\+65[\s-]?)?[3689]\d{3}[\s-]?\d{4}(?![\s-]?\d)"), "[PHONE]"),
    ("unit", re.compile(r"#\d{1,3}-\d{1,5}\b"), "[UNIT]"),
]


def redact_text(text):
    """Return (clean_text, {kind: count})."""
    found = {}
    for kind, pattern, repl in PATTERNS:
        def sub(m, kind=kind, repl=repl):
            new = repl(m) if callable(repl) else repl
            if new != m.group():
                found[kind] = found.get(kind, 0) + 1
            return new
        text = pattern.sub(sub, text)
    return text, found


def redact(value):
    """Redact strings anywhere inside dicts and lists. Returns (clean_value, total_count)."""
    if isinstance(value, str):
        clean, found = redact_text(value)
        return clean, sum(found.values())
    if isinstance(value, dict):
        out, total = {}, 0
        for k, v in value.items():
            out[k], n = redact(v)
            total += n
        return out, total
    if isinstance(value, (list, tuple)):
        out, total = [], 0
        for v in value:
            c, n = redact(v)
            out.append(c)
            total += n
        return out, total
    return value, 0