"""Complaint emails and their labels, from whichever merlion_clean is connected.

  fixture   labels come from the fixture's eval_labels table (synthetic ground truth)
  mysql     the 90 real emails from complaint_emails; labels from golden/labels.json, which the team
            fills in by hand. The file holds IDs and verdicts only (no personal data), so it is safe
            to commit. `python -m merlion_obs.dataset init` writes a blank one for every email.

A label: {"email_id", "system_version", "order_id", "category", "resolution", "amount"}
  category    not_received | late | damaged | overcharged | corporate | no_number
  resolution  redeliver | refund | compensate | reject | clarify | refuse | escalate
Only labelled emails are scored. The harness checklist asks for >= 20 eval questions with at least
two refusals: the corporate-account emails (audit T27) are the refusals.
"""
import json
import sys
from pathlib import Path

from .db import ROOT, connect
from .reference import SGT

LABELS = ROOT / "golden" / "labels.json"


def _labels(ops):
    if ops.kind == "sqlite":
        return {r["email_id"]: r for r in ops.all("SELECT * FROM eval_labels")}
    if not LABELS.exists():
        return {}
    return {r["email_id"]: r for r in json.loads(LABELS.read_text(encoding="utf-8")) if r.get("resolution")}


def load(ops, labelled_only=False):
    labels = _labels(ops)
    out = []
    for r in ops.all("SELECT email_id, received_at, from_email, subject, body_text FROM complaint_emails ORDER BY received_at, email_id"):
        lab = labels.get(r["email_id"])
        truth = None
        if lab:
            truth = {"system": lab.get("system_version"), "order_no": lab.get("order_id"), "category": lab["category"],
                     "resolution": lab["resolution"], "amount": lab.get("amount"), "driver_id": lab.get("driver_code"),
                     "zone": lab.get("zone")}
        if labelled_only and not truth:
            continue
        out.append({"email_id": r["email_id"], "received_at": r["received_at"].replace(tzinfo=SGT), "from_email": r["from_email"],
                    "subject": r["subject"] or "", "body": r["body_text"] or "", "truth": truth})
    return out


def init_labels():
    """Write golden/labels.json with one blank label per email in the connected database."""
    ops = connect()
    existing = {r["email_id"]: r for r in (json.loads(LABELS.read_text(encoding="utf-8")) if LABELS.exists() else [])}
    rows = []
    for r in ops.all("SELECT email_id, order_id_mentioned FROM complaint_emails ORDER BY email_id"):
        rows.append(existing.get(r["email_id"]) or {"email_id": r["email_id"], "system_version": None,
                                                     "order_id": r["order_id_mentioned"], "category": None,
                                                     "resolution": None, "amount": None, "notes": ""})
    LABELS.parent.mkdir(exist_ok=True)
    LABELS.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"{LABELS}: {len(rows)} emails, {sum(1 for r in rows if r.get('resolution'))} labelled")


if __name__ == "__main__":
    if sys.argv[1:] == ["init"]:
        init_labels()
    else:
        print(__doc__)
