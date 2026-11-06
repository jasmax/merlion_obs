"""Reference data and assumptions shared by the tools, the checks and the fixture.

Everything here mirrors the team repo (team2_merlion_express), so the harness judges the
app against the same rules the Spring tools use:

  TOWN_ZONE       ingestion/ingest_town_zone_map.py   (zone comes from the town, audit T04)
  SECTOR_TOWNS    ingestion/ingest_orders.py          (URA postal districts, used only to show T04)
  GPS_TO_SGT      ingestion/ingest_gps_pings.py       (Singapore time = GPS ts + 16 h, audit T16)
  RATE_CARD_*     ingestion/ingest_rate_card*.py      (card starts 1 Apr 2026, audit T24)

TOWN_CENTROID is our own assumption: the clean schema has no geocode for an address, so
getGpsTrail can only say whether the driver was in the delivery *town* at delivery time.
"""
import datetime as dt
import math

SGT = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc

V2_SWITCH = dt.date(2026, 2, 1)           # v1 -> v2 migration (orders numbered from 20001 again)
ORDER_ID_RANGE = (20001, 34353)           # ingest_complaint_emails.py
GPS_TO_SGT = dt.timedelta(hours=16)       # audit T16: the GPS clock runs 8 h behind true UTC
GPS_WINDOW = dt.timedelta(minutes=30)
GPS_NEAR_M = 2000                         # "in the delivery town": within 2 km of the town centre
RATE_CARD_START = dt.date(2026, 4, 1)
GST = 0.09

TOWN_ZONE = {
    **{t: "Central" for t in ["Bishan", "Bukit Merah", "Geylang", "Queenstown", "Toa Payoh"]},
    **{t: "East / North-East" for t in ["Ang Mo Kio", "Bedok", "Hougang", "Pasir Ris", "Punggol",
                                         "Sengkang", "Serangoon", "Tampines"]},
    **{t: "North / West" for t in ["Bukit Batok", "Choa Chu Kang", "Clementi", "Jurong East",
                                    "Jurong West", "Woodlands", "Yishun"]},
}
ZONES = ["Central", "East / North-East", "North / West", "Sentosa / Jurong Island"]

TOWN_CENTROID = {  # approximate town centres (lat, lon); team assumption, see module docstring
    "Ang Mo Kio": (1.3691, 103.8454), "Bedok": (1.3236, 103.9273), "Bishan": (1.3526, 103.8352),
    "Bukit Batok": (1.3590, 103.7637), "Bukit Merah": (1.2819, 103.8239), "Choa Chu Kang": (1.3840, 103.7470),
    "Clementi": (1.3162, 103.7649), "Geylang": (1.3201, 103.8918), "Hougang": (1.3612, 103.8863),
    "Jurong East": (1.3329, 103.7436), "Jurong West": (1.3404, 103.7090), "Pasir Ris": (1.3721, 103.9474),
    "Punggol": (1.3984, 103.9072), "Queenstown": (1.2942, 103.7861), "Sengkang": (1.3868, 103.8914),
    "Serangoon": (1.3554, 103.8679), "Tampines": (1.3496, 103.9568), "Toa Payoh": (1.3343, 103.8563),
    "Woodlands": (1.4382, 103.7890), "Yishun": (1.4304, 103.8354),
}

_DISTRICTS = [  # postal sectors -> towns the district covers (ingest_orders.py DISTRICT_TOWNS)
    (["09", "10"], ["Bukit Merah"]), (["11", "12", "13"], ["Clementi", "Queenstown"]),
    (["14", "15", "16"], ["Queenstown", "Bukit Merah"]), (["24", "25", "26", "27"], []), (["28", "29", "30"], []),
    (["31", "32", "33"], ["Toa Payoh"]), (["34", "35", "36", "37"], ["Geylang", "Toa Payoh"]),
    (["38", "39", "40", "41"], ["Geylang"]), (["42", "43", "44", "45"], []), (["46", "47", "48"], ["Bedok"]),
    (["49", "50", "81"], ["Pasir Ris"]), (["51", "52"], ["Tampines", "Pasir Ris"]),
    (["53", "54", "55", "82"], ["Serangoon", "Hougang", "Punggol", "Sengkang"]), (["56", "57"], ["Bishan", "Ang Mo Kio"]),
    (["58", "59"], ["Clementi"]), (["60", "61", "62", "63", "64"], ["Jurong East", "Jurong West"]),
    (["65", "66", "67", "68"], ["Bukit Batok", "Choa Chu Kang"]), (["69", "70", "71"], ["Choa Chu Kang"]),
    (["72", "73"], ["Woodlands"]), (["75", "76"], ["Yishun"]), (["77", "78"], ["Ang Mo Kio", "Bishan"]), (["79", "80"], []),
]
SECTOR_TOWNS = {s: towns for sectors, towns in _DISTRICTS for s in sectors}


def zone_from_postal_sector(postal_code):
    """The naive zone: read it from the postal sector. Wrong for ~97% of orders (audit T04)."""
    towns = SECTOR_TOWNS.get((postal_code or "")[:2])
    return TOWN_ZONE[towns[0]] if towns else None


def dist_m(a_lat, a_lon, b_lat, b_lon):
    return math.hypot((a_lat - b_lat) * 111_000, (a_lon - b_lon) * 111_000 * math.cos(math.radians(1.35)))


# ------------------------------------------------------------------ assumed policy
# The client gave no written policy (BuildGuide "Data problems"). These rules are the team's
# assumption and every verdict carries "based on assumed policy". Keep in sync with docs/policies.md.
VERDICTS = ["redeliver", "refund", "compensate", "reject"]
OUTCOMES = VERDICTS + ["clarify", "refuse", "escalate"]   # clarify/refuse/escalate hand back to a human
LATE_VOUCHER_SGD = 5.00
DAMAGE_GOODWILL_SGD = 15.00
MONEY_CAP_SGD = 30.00
POLICY_SOURCE = "policies.md (assumed policy)"

NEEDS = {  # facts a verdict needs, per complaint type; a missing fact means "escalate", never a guess
    "not_received": ["delivery_outcome", "evidence_of_delivery"],
    "late": ["was_late"],
    "damaged": ["delivery_outcome"],
    "overcharged": ["expected_price", "charged"],
}


def policy(f):
    """(outcome, amount_sgd, reason) from facts. Used to label the fixture and to score the chat step."""
    if f.get("refuse"):
        return "refuse", None, "corporate account: not in any table we hold (audit T27)"
    if not f.get("order_found"):
        return "clarify", None, f.get("order_basis") or "order not identified"
    cat = f.get("category")
    if cat not in NEEDS:
        return "escalate", None, "complaint type not covered by policy"
    missing = [k for k in NEEDS[cat] if f.get(k) is None]
    if missing:
        return "escalate", None, "the data does not show: " + ", ".join(missing)
    if cat == "not_received":
        if f["delivery_outcome"] != "DELIVERED":
            return "redeliver", None, "no successful delivery on record"
        if f["evidence_of_delivery"]:
            return "reject", None, "delivery confirmed by " + f["evidence_of_delivery"]
        return "redeliver", None, "delivered in the DB but no POD or GPS evidence: benefit of the doubt"
    if cat == "late":
        return ("compensate", LATE_VOUCHER_SGD, "was_late = 1 in deliveries") if f["was_late"] \
            else ("reject", None, "delivered by the promised date")
    if cat == "damaged":
        return ("compensate", DAMAGE_GOODWILL_SGD, "delivered parcel reported damaged") \
            if f["delivery_outcome"] == "DELIVERED" else ("redeliver", None, "parcel not delivered yet")
    if cat == "overcharged":
        diff = round(f["charged"] - f["expected_price"], 2)
        return ("refund", diff, "charged above the rate card") if diff > 0.01 else ("reject", None, "charge matches the rate card")
