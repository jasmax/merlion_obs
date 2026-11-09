"""Run: python test_redact.py   (every case must pass before you trust the logs)"""
from redact import redact, redact_text

MUST_REMOVE = [
    ("Email me at wei.ling.tan@gmail.com", "Email me at [EMAIL]"),
    ("Call 9123 4567 after 6pm", "Call [PHONE] after 6pm"),
    ("my number is +65 8765-4321", "my number is [PHONE]"),
    ("landline 61234567", "landline [PHONE]"),
    ("NRIC S1234567D for the refund", "NRIC [NRIC] for the refund"),
    ("FIN g7654321x", "FIN [NRIC]"),
    ("card 4111 1111 1111 1111 was charged", "card [CARD] was charged"),
    ("Blk 123 Tampines St 11 #05-123", "Blk 123 Tampines St 11 [UNIT]"),
]
MUST_KEEP = [  # the app needs these: redacting them breaks the agent
    "order 104233 never arrived",
    "postal code 520123",
    "charged S$12.50 on 03/11/2026",
    "tracking 1234 5678 9012 3456",      # 16 digits but fails the card checksum
    "delivered at 14:32, 2 parcels",
]

fails = 0
for text, want in MUST_REMOVE:
    got, _ = redact_text(text)
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  remove  {text!r} -> {got!r}")
for text in MUST_KEEP:
    got, found = redact_text(text)
    ok = got == text
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  keep    {text!r}" + ("" if ok else f" -> {got!r} {found}"))
clean, n = redact({"to": "a@b.com", "notes": ["ring 9123 4567"], "order": "104233", "amount": 12.5})
ok = clean == {"to": "[EMAIL]", "notes": ["ring [PHONE]"], "order": "104233", "amount": 12.5} and n == 2
fails += not ok
print(f"{'PASS' if ok else 'FAIL'}  nested dict/list, {n} redactions")
print(f"\n{fails} failed" if fails else "\nall passed")
raise SystemExit(1 if fails else 0)