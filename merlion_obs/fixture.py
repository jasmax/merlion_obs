"""A synthetic merlion_clean: the real nine-table schema, filled with fake people, reproducing what the
team's data audit found in the client's files (docs/data-quality-report.md). CI and the public demo
use it because the client data never goes into Git. Point MERLION_SOURCE=mysql at the real database
for everything else.

Audit findings reproduced:
  T01  order numbers 20001.. restart in v2 on 1 Feb 2026, so most numbers exist in both systems
  T04  postal codes are well-formed but unrelated to the town in the address; zone comes from the town
  T07  some orders have no delivery record (IN_TRANSIT, LOST)
  T15  42% of delivery remarks are empty (NULL)
  T16  gps_pings.ts = ts_epoch + 16 h (already applied, as ingest_gps_pings.py does); ~5 pings/driver/day
  T21  POD scans exist only for v1 orders (80 scans)
  T23  the POD remark disagrees with deliveries.remarks on ~94% of scans; COD conflicts (O02-O04)
  T24  the rate card starts 1 Apr 2026
  T26  complaint emails give a bare order number that matches a v1 and a v2 order
  T27  some emails cite a corporate account (MERL-C-xxxx) that is in no table
Every complaint is labelled by running the *correct* pipeline on the order it was written about.
"""
import datetime as dt
import random

from .db import Ops, create_fixture_db
from .reference import (GPS_TO_SGT, GST, RATE_CARD_START, SECTOR_TOWNS, SGT, TOWN_CENTROID, TOWN_ZONE, UTC, V2_SWITCH,
                        policy)

FIRST = ["Wei Ling", "Ahmad", "Priyanka", "Jun Hao", "Siti", "Marcus", "Mei Xin", "Ravi", "Hui Min", "Daniel",
         "Nurul", "Kelvin", "Aisha", "Benjamin", "Shu Fen", "Arjun", "Pei Shan", "Farhan", "Li Ting", "Gopal"]
LAST = ["Tan", "Lim", "Rahman", "Ng", "Kumar", "Wong", "Lee", "Goh", "Ismail", "Chua", "Pillai", "Teo", "Ong",
        "Yeo", "Koh", "Singh", "Toh", "Ang"]
DB_REMARKS = ["left at riser", "handed to customer", "left at door", "left with neighbour", "security guard counter",
              "customer collected at lobby", "placed in parcel locker"]
POD_REMARKS = ["dog very fierce", "left at riser", "customer not around, left at door", "gate locked, passed to guard",
               "handed over in person", "box slightly dented", "signed by maid", "fragile sticker torn"]
ROLES = ["Neighbour", "Security", "Front desk", "Concierge"]
RATES = {"Central": [4.80, 7.50, 12.80], "East / North-East": [5.20, 8.00, 13.50], "North / West": [5.60, 8.60, 14.20],
         "Sentosa / Jurong Island": [9.80, 14.00, 22.00]}
SAME_DAY = {"Central": 6.00, "East / North-East": 6.00, "North / West": 7.50, "Sentosa / Jurong Island": None}
BANDS = [(0, 3), (3, 10), (10, 25)]
RULES = [("cod_handling", "COD handling: 1.5% of collected amount, min $2.00", 0.015, "percent"),
         ("cod_handling_min", "COD handling: 1.5% of collected amount, min $2.00", 2.00, "sgd"),
         ("reattempt_fee", "Failed delivery re-attempt: $3.00 from 3rd attempt onwards", 3.00, "sgd"),
         ("public_holiday", "Public holidays: no standard delivery. Express PH rate = 2x standard.", 2, "multiplier"),
         ("gst", "All rates before GST (9%).", 0.09, "percent"),
         ("volume_rebate", "Corporate accounts: volume rebate 5% above 2,000 parcels/mth.", 0.05, "percent")]
ZONE_TOWNS = {z: [t for t, zz in TOWN_ZONE.items() if zz == z] for z in set(TOWN_ZONE.values())}
SECTORS = [s for s in SECTOR_TOWNS if s != "74"]
RISKY = {"DRV007": 3.0, "DRV021": 2.4}  # two drivers with genuinely worse service, so the weekly view has signal

TEMPLATES = {
    "not_received": [
        "Hi, my parcel for order {no} shows delivered{when} but I never got it. Nobody came to my door.",
        "Order {no}: the tracking says delivered{when}, but there is nothing here. Where is my parcel?",
        "I did not receive order {no}. It was marked delivered{when}. Please check with your driver.",
    ],
    "late": [
        "Order {no} was supposed to arrive earlier but it was delivered late{when}. Very disappointing.",
        "My parcel {no} came days after the promised date{when}. Why the delay?",
    ],
    "damaged": [
        "My package for order #{no} arrived{when} with the box crushed and the item inside broken.",
        "Order {no} was delivered{when} but the contents are damaged. Please advise.",
    ],
    "overcharged": [
        "I was charged ${charged:.2f} for delivery of order {no}{when}. That seems too much for a parcel this size.",
        "For order {no}{when} I was billed ${charged:.2f}. Please check the rate card and refund the difference.",
    ],
    "corporate": [
        "Our corporate account {corp} has had three late deliveries this month. Please look into it urgently.",
        "Please send the delivery report for corporate account {corp}. Several parcels are missing.",
    ],
    "no_number": ["I never got my parcel and I can't find the order number. Nobody came. Please help."],
}
SUBJECTS = {"not_received": "Parcel not received", "late": "Late delivery", "damaged": "Damaged parcel",
            "overcharged": "Overcharged", "corporate": "Corporate account", "no_number": "Missing parcel"}


def _sgt_epoch(ts):
    return int((ts - GPS_TO_SGT).replace(tzinfo=UTC).timestamp())


def _iso(x):
    return x.isoformat(sep=" ") if isinstance(x, dt.datetime) else x.isoformat() if isinstance(x, dt.date) else x


def _expected_price(zone, weight):
    i = next(i for i, (lo, hi) in enumerate(BANDS) if lo < weight <= hi)
    return round(RATES[zone][i] * (1 + GST), 2)


def build(path, start=dt.date(2026, 5, 4), days=28, seed=7):
    """Create the fixture DB at `path` and return the labelled complaint stream."""
    rng = random.Random(seed)
    conn = create_fixture_db(path)

    # ---- drivers (T08: four names shared by two driver codes) ----
    names = rng.sample([f"{f} {l}" for f in FIRST for l in LAST], 36)
    names += names[:4]
    zones = ["Central", "East / North-East", "North / West"]
    drivers = []
    for i in range(1, 41):
        drivers.append((i, f"DRV{i:03d}", names[i - 1], f"9{rng.randint(1000000, 9999999)}",
                        rng.choice(["motorcycle", "van", "van", "lorry"]), _iso(dt.date(2024, 1, 1) + dt.timedelta(days=rng.randint(0, 600)))))
    drv_zone = {d[1]: zones[(d[0] - 1) % 3] for d in drivers}
    drv_id = {d[1]: d[0] for d in drivers}
    by_zone = {z: [c for c, zz in drv_zone.items() if zz == z] for z in zones}
    conn.executemany("INSERT INTO drivers VALUES (?,?,?,?,?,?)", drivers)
    conn.executemany("INSERT INTO town_zone_map VALUES (?,?,?,?)",
                     [(t, z, 0, "Team assumption (4 Nov 2026), from Singapore planning regions; not provided by the client")
                      for t, z in TOWN_ZONE.items()])

    # ---- orders and deliveries, v1 (1 Oct 2025 - 31 Jan 2026) and v2 (1 Feb - 30 Jun 2026) ----
    orders, dels, dkey = {}, [], {}
    end = dt.date(2026, 6, 30)
    did = 0
    for sv, d0, d1, lo, hi in (("v1", dt.date(2025, 10, 1), V2_SWITCH - dt.timedelta(days=1), 90, 110),
                               ("v2", V2_SWITCH, end, 76, 94)):
        oid, day = 20000, d0
        while day <= d1:
            for _ in range(rng.randint(lo, hi)):
                oid += 1
                town = rng.choice(list(TOWN_ZONE))
                zone = TOWN_ZONE[town]
                sector = rng.choice(SECTORS)
                postal = f"{sector}{rng.randint(0, 9999):04d}"
                name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                promised = day + dt.timedelta(days=rng.randint(1, 3))
                status = "DELIVERED"
                if promised >= end - dt.timedelta(days=2) or rng.random() < 0.02:
                    status = "IN_TRANSIT"
                elif rng.random() < 0.01:
                    status = "LOST"
                o = dict(system_version=sv, order_id=oid, order_date=day, customer_name=name,
                         customer_phone=f"{rng.choice('89')}{rng.randint(1000000, 9999999)}",
                         delivery_address=f"Blk {rng.randint(1, 999)} {town} Street {rng.randint(1, 90)}, "
                                          f"#{rng.randint(2, 20):02d}-{rng.randint(1, 300):03d}, Singapore {postal}",
                         postal_code=postal, postal_code_clean=postal, postal_code_valid=1, postal_code_recovered=0, town=town,
                         postal_town_mismatch=int(town not in SECTOR_TOWNS[sector]), zone=zone,
                         parcel_weight_kg=round(rng.uniform(0.3, 24.5), 2),
                         cod_amount=round(rng.uniform(8, 120), 2) if rng.random() < 0.2 else 0.0, promised_date=promised, status=status)
                orders[(sv, oid)] = o
                if status == "DELIVERED":
                    drv = rng.choice(by_zone[zone])
                    late = rng.random() < 0.06 * RISKY.get(drv, 1)
                    dday = promised + dt.timedelta(days=rng.randint(1, 2)) if late else promised - dt.timedelta(days=rng.choice([0, 0, 1]))
                    when = dt.datetime.combine(max(dday, day), dt.time(rng.randint(8, 21), rng.randint(0, 59)))
                    if rng.random() < 0.06:
                        did += 1
                        dels.append([did, sv, oid, drv_id[drv], when - dt.timedelta(days=1), "FAILED", 0, "customer not home", 1])
                    did += 1
                    row = [did, sv, oid, drv_id[drv], when, "DELIVERED", int(when.date() > promised),
                           rng.choice(DB_REMARKS) if rng.random() > 0.42 else None, 1]
                    dels.append(row)
                    dkey[(sv, oid)] = row
            day += dt.timedelta(days=1)
    code_of = {d[0]: d[1] for d in drivers}

    # ---- GPS background: ~5 pings per driver per day, 07:00-22:59 SGT, around the driver's towns ----
    pings = []
    day = dt.date(2025, 10, 1)
    while day <= end:
        for code, zone in drv_zone.items():
            for _ in range(rng.randint(3, 7)):
                ts = dt.datetime.combine(day, dt.time(rng.randint(7, 22), rng.randint(0, 59), rng.randint(0, 59)))
                lat, lon = TOWN_CENTROID[rng.choice(ZONE_TOWNS[zone])]
                pings.append((code, drv_id[code], ts, lat + rng.uniform(-0.012, 0.012), lon + rng.uniform(-0.012, 0.012)))
        day += dt.timedelta(days=1)

    # ---- complaint stream ----
    v2_recent = sorted((k for k, r in dkey.items() if k[0] == "v2"), key=lambda k: dkey[k][4])
    v1_collide = [k for k in dkey if k[0] == "v1" and ("v2", k[1]) in orders]
    v2_old = [k for k in dkey if k[0] == "v2" and orders[k]["order_date"] < RATE_CARD_START]
    cat_w = {"not_received": 28, "late": 24, "damaged": 14, "overcharged": 16, "corporate": 8, "no_number": 3}
    used, pods, emails = set(), {}, []
    for d in range(days):
        day = start + dt.timedelta(days=d)
        n = rng.randint(13, 19) if day.weekday() < 5 else rng.randint(5, 8)
        seq = 0
        for _ in range(n):
            seq += 1
            at = dt.datetime.combine(day, dt.time(rng.randint(8, 19), rng.randint(0, 59)))
            cat = rng.choices(list(cat_w), list(cat_w.values()))[0]
            key = None
            if cat in ("not_received", "late", "damaged") and rng.random() < 0.25:
                pool = v1_collide
            elif cat == "overcharged" and rng.random() < 0.25:
                pool = v2_old
            elif cat in ("corporate", "no_number"):
                pool = None
            else:
                lo_t, hi_t = at - dt.timedelta(days=8), at - dt.timedelta(hours=6)
                pool = [k for k in v2_recent if lo_t <= dkey[k][4] <= hi_t]
            if pool:
                cands = [k for k in rng.sample(pool, min(60, len(pool))) if k not in used]
                weights = [RISKY.get(code_of[dkey[k][3]], 1) for k in cands]
                key = rng.choices(cands, weights)[0]
                used.add(key)
            o = orders[key] if key else None
            dl = dkey.get(key)
            charged = None
            if key and cat == "late":
                promised = o["promised_date"]
                if rng.random() < 0.7:
                    nt = dt.datetime.combine(promised + dt.timedelta(days=rng.randint(1, 2)), dt.time(rng.randint(10, 20), rng.randint(0, 59)))
                    if nt < at - dt.timedelta(hours=2):
                        dl[4], dl[6] = nt, 1
                else:
                    nt = dt.datetime.combine(promised, dt.time(rng.randint(9, 18), rng.randint(0, 59)))
                    if nt < at:
                        dl[4], dl[6] = nt, 0
            if key and cat == "not_received":
                town = o["town"]
                if rng.random() < (0.5 if key[0] == "v2" else 0.3):
                    for m in (-6, -2, 3):
                        lat, lon = TOWN_CENTROID[town]
                        pings.append((code_of[dl[3]], dl[3], dl[4] + dt.timedelta(minutes=m),
                                      lat + rng.uniform(-0.003, 0.003), lon + rng.uniform(-0.003, 0.003)))
                if key[0] == "v1" and rng.random() < 0.6:
                    pods[key] = None
            if key and cat == "damaged" and key[0] == "v1" and rng.random() < 0.5:
                pods[key] = None
            if key and cat == "overcharged":
                right = _expected_price(o["zone"], o["parcel_weight_kg"])
                charged = round(right + rng.choice([0, 0, 0, 2.18, 3.27, 4.36, 5.45, 6.54]), 2)
            # clues the customer happens to give
            phone = o and rng.random() < 0.6
            full = o and rng.random() < 0.75
            hint = ""
            if o and rng.random() < 0.75:
                ref = dl[4] if dl else dt.datetime.combine(o["order_date"], dt.time())
                hint = f" on {ref.day} {ref:%B}" if key[0] == "v2" or rng.random() < 0.5 else f" back in {ref:%B}"
            sig_name = o["customer_name"] if full else (o["customer_name"].split()[0] if o else f"{rng.choice(FIRST)} {rng.choice(LAST)}")
            corp = f"MERL-C-{rng.randint(1000, 9999)}"
            body = rng.choice(TEMPLATES[cat]).format(no=o["order_id"] if o else "", when=hint, charged=charged or 0, corp=corp)
            if phone:
                body += f" You can reach me at {o['customer_phone'][:4]} {o['customer_phone'][4:]}."
            body += f"\n\nThanks,\n{sig_name}"
            handle = sig_name.split()[0].lower() + str(rng.randint(1, 99))
            subject = SUBJECTS[cat] + (f" - order {o['order_id']}" if o and rng.random() < 0.6 else "")
            emails.append({"email_id": f"{day:%Y-%m-%d}_{seq:03d}", "received_at": at.replace(tzinfo=SGT),
                           "from_email": f"{handle}@{rng.choice(['gmail.com', 'yahoo.com.sg', 'hotmail.com'])}",
                           "subject": subject, "body": body, "_key": key, "_cat": cat})

    # ---- POD scans: complaint v1 orders first, then random v1 deliveries, 80 in all ----
    others = [k for k in dkey if k[0] == "v1" and k not in used]
    for k in rng.sample(others, 80 - min(80, len(pods))):
        pods[k] = None
    pod_rows = []
    for k in list(pods)[:80]:
        o, dl = orders[k], dkey[k]
        db_remark = dl[7]
        if rng.random() < 0.06:
            remark = db_remark
        elif o["cod_amount"] and rng.random() < 0.5:
            remark = f"COD collected ${o['cod_amount'] + rng.choice([0, 0, 5]):.2f}"
        else:
            remark = rng.choice([r for r in POD_REMARKS if r != db_remark])
        received = o["customer_name"] if rng.random() < 0.67 else rng.choice(ROLES)
        pod_rows.append((f"POD_v1_{k[1]}.jpg", "v1", k[1], _iso(dl[4]), code_of[dl[3]], o["delivery_address"], received, remark, 1,
                         f"MERLION EXPRESS\nProof of Delivery\nOrder Ref: V1-{k[1]}\nDriver: {code_of[dl[3]]}\n"
                         f"Received by: {received}\nRemarks: {remark}", "ocr+rules", "2026-11-03 14:00:00", 1))

    # ---- persist ----
    conn.executemany("INSERT INTO orders VALUES (" + ",".join("?" * 17) + ")",
                     [tuple(_iso(v) for v in o.values()) for o in orders.values()])
    conn.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?)", [tuple(_iso(v) for v in r) for r in dels])
    conn.executemany("INSERT INTO gps_pings(driver_code, driver_id, ts_epoch, ts, lat, lon, speed_kmh) VALUES (?,?,?,?,?,?,?)",
                     [(c, i, _sgt_epoch(ts), _iso(ts.replace(microsecond=0)), round(la, 6), round(lo, 6), round(rng.uniform(0, 60), 1))
                      for c, i, ts, la, lo in sorted(pings, key=lambda p: (p[0], p[2]))])
    conn.executemany("INSERT INTO pod_extracts(source_file, system_version, order_id, pod_datetime, driver_code, delivered_to_address,"
                     " received_by, pod_remarks, has_signature, raw_text, extract_method, extracted_at, order_match_ok) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", pod_rows)
    conn.executemany("INSERT INTO rate_card(effective_from, zone, weight_min_kg, weight_max_kg, base_rate_sgd, same_day_surcharge_sgd,"
                     " cbd_erp_surcharge_sgd, source_file) VALUES (?,?,?,?,?,?,?,?)",
                     [("2026-04-01", z, lo, hi, p, SAME_DAY[z], cbd, "rate_card_apr2026.jpg")
                      for z, ps in RATES.items() for (lo, hi), p, cbd in zip(BANDS, ps, [1.5, 1.5, 2.5])])
    conn.executemany("INSERT INTO rate_card_rules(effective_from, rule_name, rule_text, value, unit) VALUES ('2026-04-01',?,?,?,?)", RULES)
    import re
    conn.executemany("INSERT INTO complaint_emails(email_id, received_at, from_email, subject, body_text, order_id_mentioned) VALUES (?,?,?,?,?,?)",
                     [(e["email_id"], _iso(e["received_at"].replace(tzinfo=None)), e["from_email"], e["subject"], e["body"],
                       (lambda ns: ns.pop() if len(ns) == 1 else None)({int(x) for x in re.findall(r"(?<![\d-])(\d{5})(?!\d)", e["subject"] + "\n" + e["body"])}))
                      for e in emails])
    conn.commit()

    # ---- labels: the correct pipeline on the order each email was written about ----
    from .agent import ORACLE, extract_clues, oracle
    from .tools import resolve_order
    ops = Ops(conn, "sqlite", "fixture:merlion_clean")
    labels = []
    for e in emails:
        key, cat = e.pop("_key"), e.pop("_cat")
        truth = {"system": key[0] if key else None, "order_no": key[1] if key else None, "category": cat,
                 "driver_id": code_of[dkey[key][3]] if key in dkey else None, "zone": orders[key]["zone"] if key else None}
        if cat == "corporate":
            res, amt = "refuse", None
        elif not key:
            res, amt = "clarify", None
        else:
            clues = extract_clues(e)
            r = resolve_order(ops, key[1], clues, e["received_at"].replace(tzinfo=None), ORACLE["prompts"]["resolveOrder"])
            if r["data"]["status"] != "RESOLVED":
                res, amt = "clarify", None
            else:
                facts = oracle(ops, e, {"system_version": key[0], "order_id": key[1], "category": cat})["facts"]
                res, amt, _ = policy(facts)
        truth.update(resolution=res, amount=amt)
        e["truth"] = truth
        labels.append((e["email_id"], truth["system"], truth["order_no"], cat, res, amt, truth["driver_id"], truth["zone"]))
    conn.executemany("INSERT INTO eval_labels VALUES (?,?,?,?,?,?,?,?)", labels)
    conn.commit()
    conn.close()
    return emails
