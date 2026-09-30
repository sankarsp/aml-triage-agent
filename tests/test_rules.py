from datetime import datetime, timedelta, timezone

from app import rules

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def txn(i, day, amount, direction="in", channel="cash", cc="US"):
    return {"txn_id": f"T{i}", "ts": T0 + timedelta(days=day), "amount": amount,
            "direction": direction, "channel": channel, "counterparty_country": cc}


def test_structuring_detected():
    txns = [txn(1, 0, 9500), txn(2, 2, 9700), txn(3, 5, 9900), txn(4, 30, 2000)]
    sig = rules.detect_structuring(txns, 10_000)
    assert sig and sig.code == "STRUCTURING"
    assert sig.evidence_txn_ids == ["T1", "T2", "T3"]


def test_structuring_needs_tight_window():
    txns = [txn(1, 0, 9500), txn(2, 20, 9700), txn(3, 40, 9900)]
    assert rules.detect_structuring(txns, 10_000) is None


def test_rapid_movement():
    txns = [txn(1, 0, 50_000, channel="wire"), txn(2, 1, 45_000, "out", "wire")]
    assert rules.detect_rapid_movement(txns).code == "RAPID_MOVEMENT"


def test_high_risk_jurisdiction():
    txns = [txn(1, 0, 1000, "out", "wire", cc="kp")]
    assert rules.detect_high_risk_jurisdiction(txns, {"KP"}).code == "HIGH_RISK_JURISDICTION"


def test_clean_customer_scores_zero():
    cust = {"expected_monthly_volume": 100_000, "pep": False, "risk_rating": "low"}
    txns = [txn(i, i, 1234.56, channel="ach") for i in range(20)]
    signals, score = rules.assess(cust, txns)
    assert signals == [] and score == 0
