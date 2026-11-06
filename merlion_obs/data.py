"""Synthetic Merlion ops data that reproduces the client brief's landmines.

Swap this module for a loader over the real data pack (sql/merlion_ops.sql,
logs/gps_pings_sample.jsonl, ...) — the agent and checks only depend on the
table shapes created here.

Landmines reproduced:
  1. Order numbers repeat across systems: v1 (pre-Feb-2026) and v2 both number
     from 100001, so most complaint order numbers match one order in each.
  2. GPS vs delivery times: GPS pings are UTC; both systems store delivery time
     as *naive local SGT* (v1 also as dd/mm/yyyy). Read naively, they are 8h apart.
  3. Postal codes: ~7% of addresses lost their leading zero (5 digits) or carry
     an invalid sector.
"""
import datetime as dt
import math
import random
import sqlite3

SGT = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc

ZONES = {  # zone -> (centroid lat, lng, postal sectors)
    "Central": (1.290, 103.850, list(range(1, 31))),
    "East": (1.340, 103.940, list(range(38, 53))),
    "North-East": (1.370, 103.890, list(range(53, 58)) + list(range(79, 83))),
    "North": (1.430, 103.790, [72, 73, 75, 76, 77, 78]),
    "West": (1.350, 103.720, list(range(58, 72))),
}
SECTOR_ZONE = {s: z for z, (_, _, ss) in ZONES.items() for s in ss}
DRIVERS = {f"D{i:02d}": z for i, z in enumerate(
    ["Central", "Central", "Central", "East", "East", "East", "West", "West",
     "West", "North-East", "North-East", "North", "North", "Central"], start=1)}
# Two drivers with genuinely worse service, so the weekly view has signal.
RISKY_DRIVERS = {"D07": 3.0, "D05": 2.2}

RATE_CARDS = [  # effective_from, standard, express, same_day, late_voucher, damage_cap
    ("2026-01-01", 6.00, 11.00, 16.00, 5.00, 100.00),
    ("2026-07-01", 6.50, 12.00, 18.00, 5.00, 100.00),
]
SLA_HOURS = {"standard": 48, "express": 4, "same_day": 10}

FIRST = ["Wei Ling", "Ahmad", "Priyanka", "Jun Hao", "Siti", "Marcus", "Mei Xin", "Ravi",
         "Hui Min", "Daniel", "Nurul", "Kelvin", "Aisha", "Benjamin", "Shu Fen", "Arjun"]
LAST = ["Tan", "Lim", "Rahman", "Ng", "Kumar", "Wong", "Lee", "Goh", "Ismail", "Chua",
        "Pillai", "Teo", "Ong", "Yeo", "Koh", "Singh"]

SCHEMA = """
CREATE TABLE jobs_v1(job_no INTEGER, cust_name TEXT, cust_email TEXT, cust_tel TEXT,
  postcode TEXT, addr TEXT, svc TEXT, amount REAL, drv TEXT, created TEXT,
  done_time TEXT, status TEXT);
CREATE TABLE refunds_v1(job_no INTEGER, amount REAL, requested_at TEXT, status TEXT);
CREATE TABLE orders_v2(order_no INTEGER, customer_name TEXT, customer_email TEXT,
  customer_phone TEXT, postal_code TEXT, address TEXT, service TEXT, billed_service TEXT,
  fee_billed REAL, declared_value REAL, driver_id TEXT, created_at TEXT, promised_by TEXT,
  delivered_at TEXT, status TEXT, addr_lat REAL, addr_lng REAL);
CREATE TABLE pod_v2(order_no INTEGER, scan_id TEXT, captured_at TEXT,
  damage_visible INTEGER);
CREATE TABLE gps_pings(driver_id TEXT, ts_utc TEXT, lat REAL, lng REAL);
CREATE INDEX ix_gps ON gps_pings(driver_id, ts_utc);
CREATE TABLE geo_postal(postal_code TEXT PRIMARY KEY, lat REAL, lng REAL);
CREATE TABLE geo_address(address TEXT, lat REAL, lng REAL);
CREATE TABLE rate_cards(effective_from TEXT, standard REAL, express REAL,
  same_day REAL, late_voucher REAL, damage_cap REAL);
"""


def card_for(day: str):
    row = None
    for c in RATE_CARDS:
        if c[0] <= day:
            row = c
    return dict(zip(["effective_from", "standard", "express", "same_day",
                     "late_voucher", "damage_cap"], row))


def policy(f):
    """Merlion resolution policy. Applied to ground-truth facts to label the
    golden set, and to the agent's *observed* facts to make its recommendation —
    so every agent error traces back to a wrong fact, which is what we observe."""
    cat = f.get("category")
    needs = {"not_received": ["has_pod", "gps_at_address"], "late": ["late_hours", "card"],
             "damaged": ["damage_visible", "declared_value", "card"],
             "overcharged": ["billed_service", "card"], "refund_followup": []}
    if not f.get("order_found") or any(k not in f for k in needs.get(cat, [])):
        return "escalate", None
    if cat == "not_received":
        # Reject only when both POD and GPS confirm; otherwise benefit of the doubt.
        return ("reject", None) if f["has_pod"] and f["gps_at_address"] else ("redeliver", None)
    if cat == "late":
        if f["late_hours"] <= 0:
            return "reject", None
        if f["service"] in ("express", "same_day"):
            return "refund", round(f["fee_billed"] - f["card"]["standard"], 2)
        return ("compensate", f["card"]["late_voucher"]) if f["late_hours"] >= 48 else ("reject", None)
    if cat == "damaged":
        if f["damage_visible"]:
            return "compensate", min(f["declared_value"], f["card"]["damage_cap"])
        return "reject", None
    if cat == "overcharged":
        if f["billed_service"] != f["service"]:
            return "refund", round(f["fee_billed"] - f["card"][f["service"]], 2)
        return "reject", None
    if cat == "refund_followup":
        return ("refund", f["pending_refund"]) if f.get("pending_refund") else ("reject", None)
    return "escalate", None


def _addr(rng, zone):
    lat, lng, sectors = ZONES[zone]
    sector = rng.choice(sectors)
    code = f"{sector:02d}{rng.randint(0, 9999):04d}"
    a_lat, a_lng = lat + rng.uniform(-0.02, 0.02), lng + rng.uniform(-0.02, 0.02)
    blk = rng.randint(1, 999)
    return code, f"Blk {blk} Street {rng.randint(1, 90)}, #{rng.randint(2, 20):02d}-{rng.randint(1, 300):03d}", a_lat, a_lng


def _corrupt_postal(rng, code):
    if code.startswith("0") or rng.random() < 0.5:
        return code.lstrip("0")[:5] if code.startswith("0") else code[1:]  # 5 digits
    return f"{rng.choice([74, 83, 91, 99])}{code[2:]}"  # invalid sector


def _person(rng):
    first, last = rng.choice(FIRST), rng.choice(LAST)
    handle = f"{first.split()[0].lower()}.{last.lower()}{rng.randint(1, 99)}"
    phone = f"{rng.choice('89')}{rng.randint(100, 999)} {rng.randint(1000, 9999)}"
    return f"{first} {last}", f"{handle}@{rng.choice(['gmail.com', 'yahoo.com.sg', 'hotmail.com'])}", phone


def _dist_m(a_lat, a_lng, b_lat, b_lng):
    return math.hypot((a_lat - b_lat) * 111_000, (a_lng - b_lng) * 111_000 * math.cos(math.radians(1.35)))


def build(db_path, start=dt.date(2026, 10, 5), days=28, seed=7):
    """Create the ops DB and return the labelled complaint stream."""
    rng = random.Random(seed)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO rate_cards VALUES (?,?,?,?,?,?)", RATE_CARDS)

    # ---- v1 legacy jobs (Mar 2025 – mid Feb 2026), numbered from 100001 ----
    v1, t = [], dt.datetime(2025, 3, 1, 9, 0)
    for job_no in range(100001, 128001):
        t += dt.timedelta(minutes=rng.randint(5, 20))
        drv = rng.choice(list(DRIVERS))
        code, addr, *_ = _addr(rng, DRIVERS[drv])
        name, email, phone = _person(rng)
        done = t + dt.timedelta(hours=rng.randint(3, 30))
        v1.append((job_no, name, email, phone, code, addr, rng.choice(["standard", "express"]),
                   6.0, drv, t.strftime("%d/%m/%Y %H:%M"), done.strftime("%d/%m/%Y %H:%M"), "done"))
    db.executemany("INSERT INTO jobs_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", v1)

    # ---- v2 orders (Feb 2026 onwards), numbering restarted at 100001 ----
    orders, t = {}, dt.datetime(2026, 2, 16, 8, 0)
    window_start = dt.datetime.combine(start - dt.timedelta(days=4), dt.time())
    end = dt.datetime.combine(start + dt.timedelta(days=days), dt.time())
    order_no = 100000
    while t < end:
        t += dt.timedelta(minutes=rng.randint(4, 10))  # ~100/day
        if not 8 <= t.hour < 20:
            continue
        order_no += 1
        drv = rng.choice(list(DRIVERS))
        code, addr, a_lat, a_lng = _addr(rng, DRIVERS[drv])
        true_code = code
        if rng.random() < 0.07:
            code = _corrupt_postal(rng, code)
        name, email, phone = _person(rng)
        svc = rng.choices(["standard", "express", "same_day"], [70, 20, 10])[0]
        card = card_for(t.date().isoformat())
        promised = t + dt.timedelta(hours=SLA_HOURS[svc])
        late = rng.random() < 0.05 * RISKY_DRIVERS.get(drv, 1)
        delivered = promised + (dt.timedelta(hours=rng.randint(2, 60)) if late
                                else -dt.timedelta(minutes=rng.randint(20, 200)))
        if svc == "standard" and delivered.hour < 9:
            delivered = delivered.replace(hour=rng.randint(9, 18))
        orders[order_no] = dict(
            order_no=order_no, customer_name=name, customer_email=email, customer_phone=phone,
            postal_code=code, address=addr, service=svc, billed_service=svc, fee_billed=card[svc],
            declared_value=rng.choice([25, 40, 60, 80, 150, 300]), driver_id=drv,
            created_at=t.strftime("%Y-%m-%d %H:%M:%S"), promised_by=promised.strftime("%Y-%m-%d %H:%M:%S"),
            delivered_at=delivered.strftime("%Y-%m-%d %H:%M:%S"), status="delivered",
            addr_lat=a_lat, addr_lng=a_lng, _pod=True, _arrived=True, _damage=False,
            _true_postal=true_code)

    # ---- complaint stream (business hours SGT) with ground-truth labels ----
    cat_w = {"not_received": 30, "late": 25, "damaged": 15, "overcharged": 12, "refund_followup": 10}
    recent = [o for o in orders.values() if o["delivered_at"] >= window_start.strftime("%Y-%m-%d")]
    complaints, used, refunds = [], set(), []
    for d in range(days):
        day = start + dt.timedelta(days=d)
        n = rng.randint(14, 22) if day.weekday() < 5 else rng.randint(6, 10)
        for _ in range(n):
            at = dt.datetime.combine(day, dt.time(rng.randint(8, 19), rng.randint(0, 59)), SGT)
            cat = rng.choices(list(cat_w), list(cat_w.values()))[0]
            if cat == "refund_followup":
                job = v1[rng.randrange(len(v1) - 8000, len(v1))]  # late-2025 / early-2026 jobs
                pending = rng.random() < 0.8
                if pending:
                    refunds.append((job[0], round(rng.choice([6.0, 11.0, 25.0, 40.0]), 2),
                                    job[10], "pending"))
                truth_facts = {"category": cat, "order_found": True,
                               "pending_refund": refunds[-1][1] if pending else None}
                o_sys, o_no, o_name, o_email, o_phone = "v1", job[0], job[1], job[2], job[3]
                ref_date = dt.datetime.strptime(job[10], "%d/%m/%Y %H:%M")
                drv, zone = job[8], DRIVERS[job[8]]
            else:
                pool = [o for o in recent if o["order_no"] not in used
                        and o["delivered_at"] < (at - dt.timedelta(hours=10)).strftime("%Y-%m-%d %H:%M:%S")
                        and o["delivered_at"] > (at - dt.timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S")]
                wts = [RISKY_DRIVERS.get(o["driver_id"], 1) for o in pool]
                o = rng.choices(pool, wts)[0]
                used.add(o["order_no"])
                card = card_for(o["created_at"][:10])
                if cat == "not_received":
                    genuine = rng.random() < 0.5 * RISKY_DRIVERS.get(o["driver_id"], 1) / 1.5
                    o["_pod"], o["_arrived"] = (False, False) if genuine else (True, True)
                elif cat == "damaged":
                    o["_damage"] = rng.random() < 0.7
                elif cat == "late":
                    if dt.datetime.fromisoformat(o["delivered_at"]) <= dt.datetime.fromisoformat(o["promised_by"]) and rng.random() < 0.75:
                        lateness = rng.randint(50, 70) if o["service"] == "standard" else rng.randint(2, 8)
                        o["delivered_at"] = (dt.datetime.fromisoformat(o["promised_by"]) + dt.timedelta(hours=lateness)).strftime("%Y-%m-%d %H:%M:%S")
                elif cat == "overcharged" and rng.random() < 0.75:
                    o["billed_service"] = "express" if o["service"] == "standard" else "same_day"
                    o["fee_billed"] = card[o["billed_service"]]
                late_h = (dt.datetime.fromisoformat(o["delivered_at"]) - dt.datetime.fromisoformat(o["promised_by"])).total_seconds() / 3600
                truth_facts = {"category": cat, "order_found": True, "has_pod": o["_pod"],
                               "gps_at_address": o["_arrived"], "damage_visible": o["_damage"],
                               "late_hours": late_h, "service": o["service"],
                               "billed_service": o["billed_service"], "fee_billed": o["fee_billed"],
                               "declared_value": o["declared_value"], "card": card}
                o_sys, o_no, o_name, o_email, o_phone = "v2", o["order_no"], o["customer_name"], o["customer_email"], o["customer_phone"]
                ref_date = dt.datetime.fromisoformat(o["delivered_at"])
                drv, zone = o["driver_id"], DRIVERS[o["driver_id"]]
            action, amount = policy(truth_facts)
            from_email = o_email if rng.random() < 0.85 else _person(rng)[1]
            complaints.append(dict(
                email_id=f"E{len(complaints) + 1:04d}", received_at=at, from_email=from_email,
                body=_email_body(rng, cat, o_no, o_name, o_phone, ref_date),
                truth=dict(system=o_sys, order_no=o_no, category=cat, resolution=action,
                           amount=amount, driver_id=drv, zone=zone)))

    # ---- persist v2, POD, GPS ----
    rows, pods = [], []
    for o in orders.values():
        rows.append(tuple(o[k] for k in ["order_no", "customer_name", "customer_email", "customer_phone",
                                          "postal_code", "address", "service", "billed_service", "fee_billed",
                                          "declared_value", "driver_id", "created_at", "promised_by",
                                          "delivered_at", "status", "addr_lat", "addr_lng"]))
        if o["_pod"]:
            pods.append((o["order_no"], f"POD-{o['order_no']}.jpg", o["delivered_at"], int(o["_damage"])))
    db.executemany("INSERT INTO orders_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO pod_v2 VALUES (?,?,?,?)", pods)
    db.executemany("INSERT OR IGNORE INTO geo_postal VALUES (?,?,?)",
                   [(o["_true_postal"], o["addr_lat"], o["addr_lng"]) for o in orders.values()])
    db.executemany("INSERT INTO geo_address VALUES (?,?,?)",
                   [(o["address"], o["addr_lat"], o["addr_lng"]) for o in orders.values()])
    db.execute("CREATE INDEX ix_geo_addr ON geo_address(address)")
    db.executemany("INSERT INTO refunds_v1 VALUES (?,?,?,?)", refunds)

    pings = []
    for drv, zone in DRIVERS.items():  # background: every ~6 min, 09:00–19:00 SGT
        lat, lng, _ = ZONES[zone]
        day = window_start.date()
        while day < end.date():
            ts = dt.datetime.combine(day, dt.time(9), SGT)
            while ts.hour < 19:
                pings.append((drv, ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              lat + rng.uniform(-0.03, 0.03), lng + rng.uniform(-0.03, 0.03)))
                ts += dt.timedelta(minutes=rng.randint(4, 8))
            day += dt.timedelta(days=1)
    for o in orders.values():  # approach + dwell at the address for arrivals
        if o["order_no"] not in used or not o["_arrived"]:
            continue
        local = dt.datetime.fromisoformat(o["delivered_at"]).replace(tzinfo=SGT)
        for m in range(-12, 5, 2):
            far = max(0, -m - 3) * 0.0012
            pings.append((o["driver_id"], (local + dt.timedelta(minutes=m)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          o["addr_lat"] + far + rng.uniform(-0.0001, 0.0001), o["addr_lng"] + rng.uniform(-0.0001, 0.0001)))
    db.executemany("INSERT INTO gps_pings VALUES (?,?,?,?)", pings)
    db.commit()
    return complaints


TEMPLATES = {
    "not_received": "Hi, my parcel (order {no}) shows delivered on {date} but I never got it. Nobody came to my door. Please help.",
    "late": "Order {no} was supposed to arrive by {date} and it came way late. I paid for the service and this is unacceptable.",
    "damaged": "My package for order #{no} arrived on {date} with the box crushed and the item inside broken.",
    "overcharged": "I was charged for express on order {no} ({date}) but I only booked standard delivery. Please refund the difference.",
    "refund_followup": "Still waiting for my refund on order {no} from {date}. It's been months. When will I get my money back?",
}


def _email_body(rng, cat, no, name, phone, ref):
    date = f"{ref.day} {ref.strftime('%b')}" if cat != "refund_followup" else ref.strftime("%B")
    return (TEMPLATES[cat].format(no=no, date=date)
            + f"\n\nThanks,\n{name}\n{phone if rng.random() < 0.6 else ''}").strip()


__all__ = ["build", "policy", "card_for", "DRIVERS", "SECTOR_ZONE", "ZONES", "SGT", "UTC", "_dist_m"]
