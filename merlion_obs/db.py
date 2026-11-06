"""Read access to the cleaned database, `merlion_clean`.

Two backends, same nine tables (team repo: ingestion/create_clean_tables.sql):

  MERLION_SOURCE=mysql     the real merlion_clean on MySQL 8, local or Railway. Settings from the
                           environment or .env: MYSQL_URL (Railway) or DB_HOST, DB_PORT, DB_USER,
                           DB_PASSWORD, plus DB_NAME (default merlion_clean). Needs `pip install pymysql`.
  MERLION_SOURCE=fixture   (default) a synthetic SQLite copy of the same schema, built by fixture.py.
                           Used in CI and the demo, because client data never goes into Git.

Tools and checks write SQL with `?` placeholders and get rows back as dicts with Python types
(date, datetime, float), whichever backend is behind it. Nothing here logs row values.
"""
import datetime as dt
import os
import sqlite3
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
DATE_COLS = {"order_date", "promised_date", "joined_date", "effective_from"}
DATETIME_COLS = {"delivered_time", "ts", "pod_datetime", "received_at", "extracted_at"}

SCHEMA = """
CREATE TABLE drivers(driver_id INTEGER PRIMARY KEY, driver_code TEXT NOT NULL UNIQUE, driver_name TEXT NOT NULL,
  mobile TEXT, vehicle_type TEXT, joined_date TEXT);
CREATE TABLE town_zone_map(town TEXT PRIMARY KEY, zone TEXT NOT NULL, is_cbd INTEGER NOT NULL DEFAULT 0, source_note TEXT);
CREATE TABLE orders(system_version TEXT NOT NULL, order_id INTEGER NOT NULL, order_date TEXT, customer_name TEXT,
  customer_phone TEXT, delivery_address TEXT, postal_code TEXT, postal_code_clean TEXT,
  postal_code_valid INTEGER NOT NULL DEFAULT 0, postal_code_recovered INTEGER NOT NULL DEFAULT 0, town TEXT,
  postal_town_mismatch INTEGER, zone TEXT, parcel_weight_kg REAL, cod_amount REAL, promised_date TEXT, status TEXT,
  PRIMARY KEY(system_version, order_id));
CREATE INDEX idx_orders_order_id ON orders(order_id);
CREATE TABLE deliveries(delivery_id INTEGER PRIMARY KEY, system_version TEXT NOT NULL, order_id INTEGER NOT NULL,
  driver_id INTEGER REFERENCES drivers(driver_id), delivered_time TEXT, outcome TEXT, was_late INTEGER, remarks TEXT,
  version_match_ok INTEGER NOT NULL DEFAULT 1);
CREATE INDEX idx_deliveries_order ON deliveries(system_version, order_id);
CREATE TABLE gps_pings(ping_id INTEGER PRIMARY KEY AUTOINCREMENT, driver_code TEXT NOT NULL, driver_id INTEGER,
  ts_epoch INTEGER NOT NULL, ts TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, speed_kmh REAL);
CREATE INDEX idx_gps_driver_ts ON gps_pings(driver_code, ts);
CREATE INDEX idx_gps_driver_epoch ON gps_pings(driver_code, ts_epoch);
CREATE TABLE pod_extracts(pod_id INTEGER PRIMARY KEY AUTOINCREMENT, source_file TEXT NOT NULL UNIQUE, system_version TEXT,
  order_id INTEGER, pod_datetime TEXT, driver_code TEXT, delivered_to_address TEXT, received_by TEXT, pod_remarks TEXT,
  has_signature INTEGER, raw_text TEXT, extract_method TEXT, extracted_at TEXT NOT NULL,
  order_match_ok INTEGER NOT NULL DEFAULT 1);
CREATE INDEX idx_pod_order ON pod_extracts(system_version, order_id);
CREATE TABLE rate_card(rate_id INTEGER PRIMARY KEY AUTOINCREMENT, effective_from TEXT NOT NULL, zone TEXT NOT NULL,
  weight_min_kg REAL NOT NULL, weight_max_kg REAL NOT NULL, base_rate_sgd REAL NOT NULL, same_day_surcharge_sgd REAL,
  cbd_erp_surcharge_sgd REAL, source_file TEXT);
CREATE TABLE rate_card_rules(rule_id INTEGER PRIMARY KEY AUTOINCREMENT, effective_from TEXT NOT NULL, rule_name TEXT NOT NULL,
  rule_text TEXT NOT NULL, value REAL, unit TEXT);
CREATE TABLE complaint_emails(email_id TEXT PRIMARY KEY, received_at TEXT, from_email TEXT, subject TEXT, body_text TEXT,
  order_id_mentioned INTEGER, resolved_system_version TEXT, resolved_order_id INTEGER, match_reason TEXT, vector_doc_id TEXT);
-- Fixture only: the synthetic ground truth. For the real data, labels live in golden/labels.json.
CREATE TABLE eval_labels(email_id TEXT PRIMARY KEY, system_version TEXT, order_id INTEGER, category TEXT,
  resolution TEXT, amount REAL, driver_code TEXT, zone TEXT);
"""


def load_env(path=ROOT / ".env"):
    """Minimal .env reader (no override of variables already set). Values are never printed."""
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def _norm(col, v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if col in DATETIME_COLS and isinstance(v, str):
        return dt.datetime.fromisoformat(v)
    if col in DATE_COLS and isinstance(v, str):
        return dt.date.fromisoformat(v[:10])
    if col in DATE_COLS and isinstance(v, dt.datetime):
        return v.date()
    return v


class Ops:
    """A read-only handle on merlion_clean. `source` says which backend answered (for citations)."""

    def __init__(self, conn, kind, label):
        self.conn, self.kind, self.source = conn, kind, label

    def all(self, sql, params=()):
        if self.kind == "mysql":
            sql = sql.replace("?", "%s")
        else:  # SQLite stores dates as ISO text; compare like with like
            params = [p.isoformat(sep=" ") if isinstance(p, dt.datetime) else p.isoformat() if isinstance(p, dt.date) else p
                      for p in params]
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params))
        cols = [c[0] for c in cur.description]
        return [{c: _norm(c, v) for c, v in zip(cols, row)} for row in cur.fetchall()]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None


def _mysql_settings():
    load_env()
    url = os.environ.get("MYSQL_URL")
    if url:
        u = urlparse(url)
        return dict(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
                    password=unquote(u.password or ""), database=os.environ.get("DB_NAME") or u.path.lstrip("/") or "merlion_clean")
    missing = [k for k in ("DB_HOST", "DB_USER", "DB_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"MERLION_SOURCE=mysql needs {', '.join(missing)} (or MYSQL_URL) in the environment or .env")
    return dict(host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "3306")), user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"], database=os.environ.get("DB_NAME", "merlion_clean"))


def connect(source=None, fixture_path=None):
    """Open merlion_clean. source: 'mysql' or 'fixture' (default from MERLION_SOURCE)."""
    load_env()
    source = source or os.environ.get("MERLION_SOURCE", "fixture")
    if source == "mysql":
        try:
            import pymysql
        except ImportError:
            raise SystemExit("MERLION_SOURCE=mysql needs `pip install pymysql`")
        s = _mysql_settings()
        conn = pymysql.connect(charset="utf8mb4", connect_timeout=5, autocommit=True, **s)
        return Ops(conn, "mysql", f"mysql:{s['database']}")
    path = fixture_path or os.environ.get("MERLION_FIXTURE", str(ROOT / "out" / "merlion_clean.db"))
    if not os.path.exists(path):
        raise SystemExit(f"fixture {path} not found: run `python run_demo.py` (or fixture.build) first")
    return Ops(sqlite3.connect(path), "sqlite", "fixture:merlion_clean")


def create_fixture_db(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn
