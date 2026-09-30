"""Create the schema, load the knowledge base and generate synthetic demo data.

    python -m scripts.seed            # schema + knowledge + demo data
    python -m scripts.seed --reset    # wipe demo tables first
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db
from app.knowledge import ingest_document

ROOT = Path(__file__).resolve().parents[1]
ALERT_DATE = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
START = ALERT_DATE - timedelta(days=89)

CUSTOMERS = [
    # id, name, type, country, occupation, risk, pep, onboarded, expected monthly inbound volume
    ("C001", "Sunrise Mini Mart LLC", "business", "US", "Convenience store", "medium", False, "2021-03-02", 40_000),
    ("C002", "Daniel Reyes", "individual", "US", "Software engineer (salaried)", "low", False, "2019-07-15", 20_000),
    ("C003", "Brightsmile Dental PC", "business", "US", "Dental practice", "low", False, "2018-01-10", 90_000),
]


def _days():
    d = START
    while d < ALERT_DATE - timedelta(hours=1):
        yield d
        d += timedelta(days=1)


def build_transactions(rng: random.Random) -> list[tuple]:
    rows: list[tuple] = []
    n = 0

    def add(cid, ts, amount, direction, channel, cp=None, cc="US", desc=None):
        nonlocal n
        n += 1
        rows.append((f"T{n:05d}", cid, ts, round(amount, 2), "USD", direction, channel, cp, cc, desc))
        return f"T{n:05d}"

    # C001 convenience store: daily card settlements, twice-weekly cash, weekly suppliers
    for d in _days():
        add("C001", d + timedelta(hours=6), rng.uniform(1100, 1900), "in", "card", "Card acquirer", desc="Card settlement")
        if d.weekday() in (0, 3):
            add("C001", d + timedelta(hours=16), rng.uniform(1500, 2600), "in", "cash", desc="Cash deposit - Main St branch")
        if d.weekday() == 4:
            add("C001", d + timedelta(hours=10), rng.uniform(9000, 13500), "out", "ach", "Metro Wholesale Foods", desc="Supplier invoice")
    # ...then four deposits just under 10k at different branches, and a wire out
    for day, amt, branch in [(8, 9_450, "Main St"), (9, 9_800, "Route 22"), (11, 9_600, "Oak Ave"), (12, 9_900, "Route 22")]:
        add("C001", datetime(2026, 9, day, 17, 30, tzinfo=timezone.utc), amt, "in", "cash", desc=f"Cash deposit - {branch} branch")
    add("C001", datetime(2026, 9, 13, 11, 0, tzinfo=timezone.utc), 30_000, "out", "wire", "Coastal Ventures Ltd", "PA", "Investment")

    # C002 salaried individual: payroll, rent, card spend...
    for d in _days():
        if d.day in (1, 15):
            add("C002", d + timedelta(hours=8), 8_512.40, "in", "ach", "Nimbus Software Inc", desc="Payroll")
        if d.day == 3:
            add("C002", d + timedelta(hours=9), 2_850.00, "out", "ach", "Parkview Apartments", desc="Rent")
        if rng.random() < 0.6:
            add("C002", d + timedelta(hours=19), rng.uniform(15, 220), "out", "card", "Various merchants", desc="Card purchase")
    # ...then a large foreign wire in, immediately forwarded abroad
    add("C002", datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc), 48_000, "in", "wire", "Apex Trading FZE", "AE", "Consulting")
    add("C002", datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc), 25_000, "out", "wire", "Golden Delta Import Co", "MM", "Goods")
    add("C002", datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc), 20_000, "out", "wire", "Horizon General Trading", "AE", "Services")

    # C003 dental practice: daily insurer receipts, payroll, supplies — plus a large
    # but legitimate quarterly insurer reconciliation payment (should be closed)
    for d in _days():
        if d.weekday() < 5:
            add("C003", d + timedelta(hours=7), rng.uniform(2200, 3900), "in", "ach", "Metro Dental Insurance Co", desc="Claims reimbursement")
        if d.day in (1, 15):
            add("C003", d + timedelta(hours=9), 18_000 + rng.uniform(-400, 400), "out", "ach", "PayrollPro Services", desc="Payroll")
        if d.weekday() == 2:
            add("C003", d + timedelta(hours=11), rng.uniform(1800, 4200), "out", "ach", "DentSupply Distributors", desc="Supplies")
    add("C003", datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc), 62_340.18, "in", "ach",
        "Metro Dental Insurance Co", desc="Quarterly claims reconciliation Q3")
    return rows


ALERTS = [
    ("A-1001", "C001", "CASH_NEAR_THRESHOLD"),
    ("A-1002", "C002", "LARGE_INTL_WIRE"),
    ("A-1003", "C003", "LARGE_ACH_CREDIT"),
]


def seed(reset: bool = False) -> None:
    db.init_schema()
    with db.connection() as conn:
        if reset:
            conn.execute("TRUNCATE audit_log, case_decisions, alerts, transactions, customers, knowledge_chunks RESTART IDENTITY CASCADE")
            # Drop any LangGraph checkpoints from previous runs too (tables exist once the API has started).
            for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                conn.execute(f"DO $$ BEGIN IF to_regclass('{t}') IS NOT NULL THEN EXECUTE 'TRUNCATE {t}'; END IF; END $$")
        for c in CUSTOMERS:
            conn.execute(
                """INSERT INTO customers VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (customer_id) DO NOTHING""", c)
        rows = build_transactions(random.Random(42))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO transactions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
        for alert_id, cid, rule in ALERTS:
            conn.execute(
                """INSERT INTO alerts (alert_id, customer_id, rule_code, created_at)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (alert_id) DO NOTHING""",
                (alert_id, cid, rule, ALERT_DATE))
    chunks = 0
    for p in sorted((ROOT / "data" / "knowledge").glob("*.md")):
        kind = "policy" if p.stem.startswith("policy") else "typology"
        chunks += ingest_document(p.name, kind, p.read_text())
    print(f"Seeded {len(CUSTOMERS)} customers, {len(rows)} transactions, "
          f"{len(ALERTS)} alerts, {chunks} knowledge chunks.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    seed(ap.parse_args().reset)
