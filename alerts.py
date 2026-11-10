"""Alert rules over the recent traces. Run it on a schedule (every 15 minutes is plenty).

    python alerts.py                                  # last 60 minutes vs the 7 days before
    TRACES_DB=demo_traces.db python alerts.py         # practice week
    python alerts.py --window 30                      # a different window, in minutes

Notifies only when an alert starts or stops firing (state kept in alerts_state.json), so a long
incident sends one message, not one every 15 minutes. Set SLACK_WEBHOOK_URL in .env to post to Slack;
otherwise it prints. Exits 2 while a "page" alert is firing, so a scheduler can flag it.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from collections import Counter

from metrics import load_requests, summarize

STATE = "alerts_state.json"


def evaluate(reqs, window_min):
    now = time.time()
    recent = [r for r in reqs if r["start"] >= now - window_min * 60]
    baseline = [r for r in reqs if now - 8 * 86400 <= r["start"] < now - window_min * 60]
    day = [r for r in reqs if r["start"] >= now - 86400]
    m, b = summarize(recent), summarize(baseline)
    daily_budget = float(os.environ.get("DAILY_BUDGET_USD", float(os.environ.get("BUDGET_USD", "20")) / 30))
    guessers = {code: n for code, n in Counter(r["sender_code"] for r in recent
                                               if r["refused_by"] == "not_order_owner").items() if n >= 3}
    hour_sgt = time.gmtime(now + 8 * 3600).tm_hour

    # name: (firing?, severity, message)
    return {
        "degraded": (m["requests"] >= 5 and m["degraded_rate"] > 0.05, "page",
                     f"{m['degraded_rate']:.0%} of the last {m['requests']} complaints couldn't be checked "
                     f"(a tool or the model is failing). Baseline {b['degraded_rate']:.0%}."),
        "output_guard_blocked": (m["output_blocked"] > 0, "page",
                                 f"The output guard held {m['output_blocked']} replies: the model tried to send "
                                 f"personal data or an action that isn't allowed."),
        "order_number_guessing": (bool(guessers), "page",
                                  f"{len(guessers)} sender(s) were refused for orders they don't own "
                                  f"{max(guessers.values()) if guessers else 0} times in {window_min} min: "
                                  f"possible order-number guessing. Sender codes: {', '.join(guessers)}"),
        "escalations_high": (m["requests"] >= 5 and m["escalated_rate"] > max(0.30, 2 * b["escalated_rate"]), "ticket",
                             f"Escalation rate {m['escalated_rate']:.0%} vs baseline {b['escalated_rate']:.0%}."),
        "slow": (m["requests"] >= 5 and m["p95_ms"] > 8000, "ticket", f"p95 latency {m['p95_ms']} ms (limit 8000)."),
        "daily_spend": (summarize(day)["cost_usd"] > daily_budget, "ticket",
                        f"US${summarize(day)['cost_usd']:.4f} spent in 24 h (daily budget US${daily_budget:.4f})."),
        "no_traffic": (8 <= hour_sgt < 22 and not [r for r in reqs if r["start"] >= now - 3 * 3600], "ticket",
                       "No complaints handled in 3 hours during business hours: is the app up?"),
    }, m


def notify(text):
    print(text)
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if url:
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except OSError as e:
            print(f"(Slack post failed: {e})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=60, help="minutes to look back")
    args = ap.parse_args()
    results, m = evaluate(load_requests(), args.window)
    before = json.load(open(STATE)) if os.path.exists(STATE) else {}
    changed = False
    for name, (firing, severity, message) in results.items():
        was = before.get(name, False)
        if firing and not was:
            notify(f"[{severity.upper()}] {name}: {message}")
            changed = True
        elif was and not firing:
            notify(f"[RESOLVED] {name}")
            changed = True
    if not changed:
        print(f"No change. Last {args.window} min: {m['requests']} complaints, "
              f"{sum(f for f, _, _ in results.values())} alert(s) firing.")
    with open(STATE, "w") as f:
        json.dump({k: v[0] for k, v in results.items()}, f)
    sys.exit(2 if any(f and s == "page" for f, s, _ in results.values()) else 0)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()