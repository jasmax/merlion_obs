"""A tiny stand-in for merlion_clean that reproduces the traps from docs/data-quality-report.md.
Orders are keyed on (system_version, order_id) because order numbers repeat across v1 and v2 (T01)."""

ORDERS = {
    # T01/T26: 20078 exists in BOTH systems, for different customers.
    ("v1", "20078"): {"customer_email": "wei.ling.tan@gmail.com", "status": "DELIVERED", "order_date": "2026-02-20",
                      "driver": "DRV027"},
    ("v2", "20078"): {"customer_email": "ahmad.r@yahoo.com.sg", "status": "DELIVERED", "order_date": "2026-05-02",
                      "driver": "DRV011"},
    # T21: a v2 order, so no POD scan exists.
    ("v2", "104233"): {"customer_email": "wei.ling.tan@gmail.com", "status": "DELIVERED", "order_date": "2026-05-14",
                       "driver": "DRV007"},
    # T07: no delivery record (still in transit).
    ("v2", "31555"): {"customer_email": "wei.ling.tan@gmail.com", "status": "IN_TRANSIT", "order_date": "2026-06-10",
                      "driver": None},
    # T26, unresolvable: the same customer has 45001 in both systems.
    ("v1", "45001"): {"customer_email": "wei.ling.tan@gmail.com", "status": "DELIVERED", "order_date": "2026-01-08",
                      "driver": "DRV002"},
    ("v2", "45001"): {"customer_email": "wei.ling.tan@gmail.com", "status": "DELIVERED", "order_date": "2026-04-19",
                      "driver": "DRV019"},
}
DELIVERIES = {  # key -> (row id, remark, delivered_at)
    ("v1", "20078"): ("deliveries#73", "left at riser", "2026-02-22 12:43"),
    ("v2", "20078"): ("deliveries#18802", None, "2026-05-04 10:05"),
    ("v2", "104233"): ("deliveries#21017", "handed to customer", "2026-05-15 15:20"),
    ("v1", "45001"): ("deliveries#9120", None, "2026-01-09 11:00"),
    ("v2", "45001"): ("deliveries#16044", None, "2026-04-20 16:30"),
}
POD_SCANS = {  # only v1 orders have scans (T21). T23: the remark disagrees with the delivery record.
    ("v1", "20078"): {"file": "POD_v1_20078.jpg", "remark": "dog very fierce", "received_by": "neighbour"},
}
RATE_CARD_FROM = "2026-04-01"   # T24: the only rate card starts here