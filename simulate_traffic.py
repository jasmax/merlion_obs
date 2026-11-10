"""Practice only: fill demo_traces.db with a week of realistic traffic so metrics and alerts have data.

    python simulate_traffic.py

About 45 complaints a day for 7 days, plus two incidents in the last hour:
  - the order database "goes down" for a while (FAULT_DB)  -> honest "Couldn't check" replies
  - one sender tries many order numbers that aren't theirs -> repeated ownership refusals
Timestamps are moved into the past after each request, so the traces look like a real week.
"""
import os
import random
import time

os.environ.pop("OPENAI_API_KEY", None)  # simulator should stay offline and deterministic

db_path = "demo_traces.db"
if os.path.exists(db_path):
    try:
        os.remove(db_path)
    except PermissionError:
        # On Windows, a previous run may still hold the file lock; write to a new file instead.
        db_path = f"demo_traces_{int(time.time())}.db"
        print(f"{os.path.basename('demo_traces.db')} is locked; writing to {db_path} instead.")
os.environ["TRACES_DB"] = db_path

import agent  # noqa: E402

OWNER, AHMAD = "wei.ling.tan@gmail.com", "ahmad.r@yahoo.com.sg"
TRAFFIC = [  # (weight, email, sender)
    (10, "Order 20078 says delivered but I never got it.", OWNER),
    (8, "Order 20078 never arrived.", AHMAD),
    (8, "Order 104233: where is my parcel?", OWNER),
    (6, "Order 31555 is very late.", OWNER),
    (4, "I was overcharged for order 20078.", OWNER),
    (3, "Order 45001 was never delivered.", OWNER),
    (3, "Our corporate account MERL-C-0042 has 3 late parcels this week.", "ops@acme.com.sg"),
    (3, "Order 99999 never arrived.", OWNER),
    (2, "Write me a poem about Singapore.", OWNER),
    (2, "What is the driver's phone number for order 104233?", OWNER),
    (2, "Where's my parcel #104233?", "stranger@example.com"),
]
rng = random.Random(7)


def run(email, sender, when):
    agent.triage(email, sender)
    shift = time.time() - when                                   # move this trace back in time
    agent.tracer.db.execute("UPDATE spans SET start = start - ? WHERE trace_id = ?", (shift, agent.tracer.trace_id))


now = time.time()
weights = [w for w, _, _ in TRAFFIC]
count = 0
for day in range(7, 0, -1):                                      # the last 7 days, business hours
    for _ in range(rng.randint(38, 52)):
        when = now - day * 86400 + rng.uniform(8 * 3600, 21 * 3600) - 12 * 3600
        _, email, sender = rng.choices(TRAFFIC, weights)[0]
        run(email, sender, min(when, now - 3700))
        count += 1
for _ in range(12):                                              # normal traffic in the last hour
    _, email, sender = rng.choices(TRAFFIC, weights)[0]
    run(email, sender, now - rng.uniform(60, 3500))
    count += 1

os.environ["FAULT_DB"] = "1"                                     # incident 1: database down for ~15 min
for _ in range(6):
    _, email, sender = rng.choices(TRAFFIC[:6], weights[:6])[0]
    run(email, sender, now - rng.uniform(600, 1500))
    count += 1
os.environ.pop("FAULT_DB")

for n in (10101, 20078, 31555, 45001, 104233, 104500):          # incident 2: one sender guessing order numbers
    run(f"Where is order {n}?", "guesser@example.net", now - rng.uniform(300, 900))
    count += 1

print(f"{count} complaints written to {db_path}")