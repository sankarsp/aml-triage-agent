"""Deterministic AML risk signals.

These run before the LLM so that every case has explainable, reproducible
evidence, and so the model reasons over facts instead of raw ledgers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from decimal import Decimal

from .config import get_settings


@dataclass
class Signal:
    code: str
    severity: int  # 1 low, 2 medium, 3 high
    description: str
    evidence_txn_ids: list[str] = field(default_factory=list)

    def dict(self) -> dict:
        return asdict(self)


SEVERITY_POINTS = {1: 10, 2: 20, 3: 35}


def _amt(t: dict) -> float:
    a = t["amount"]
    return float(a) if isinstance(a, Decimal) else float(a)


def detect_structuring(txns: list[dict], threshold: float, window_days: int = 10) -> Signal | None:
    """Several cash deposits just under the reporting threshold within a short window."""
    lower = threshold * 0.8
    cands = [t for t in txns if t["channel"] == "cash" and t["direction"] == "in" and lower <= _amt(t) < threshold]
    best: list[dict] = []
    for i, t in enumerate(cands):
        window = [u for u in cands[i:] if u["ts"] - t["ts"] <= timedelta(days=window_days)]
        if len(window) > len(best):
            best = window
    if len(best) >= 3:
        total = sum(_amt(t) for t in best)
        return Signal(
            "STRUCTURING",
            3,
            f"{len(best)} cash deposits between {lower:,.0f} and {threshold:,.0f} within "
            f"{window_days} days (total {total:,.2f}); pattern consistent with avoiding the "
            f"{threshold:,.0f} reporting threshold.",
            [t["txn_id"] for t in best],
        )
    return None


def detect_rapid_movement(txns: list[dict], days: int = 3, ratio: float = 0.8) -> Signal | None:
    """Funds received and moved out again almost immediately (pass-through)."""
    ins = [t for t in txns if t["direction"] == "in" and _amt(t) >= 5_000]
    for t in ins:
        outs = [
            u for u in txns
            if u["direction"] == "out" and u["channel"] != "card"
            and t["ts"] <= u["ts"] <= t["ts"] + timedelta(days=days)
        ]
        out_total = sum(_amt(u) for u in outs)
        if out_total >= ratio * _amt(t):
            return Signal(
                "RAPID_MOVEMENT",
                3,
                f"Inbound {_amt(t):,.2f} on {t['ts']:%Y-%m-%d} followed by {out_total:,.2f} "
                f"outbound ({out_total / _amt(t):.0%}) within {days} days.",
                [t["txn_id"]] + [u["txn_id"] for u in outs],
            )
    return None


def detect_high_risk_jurisdiction(txns: list[dict], countries: set[str]) -> Signal | None:
    hits = [t for t in txns if (t.get("counterparty_country") or "").upper() in countries]
    if hits:
        cs = sorted({t["counterparty_country"].upper() for t in hits})
        total = sum(_amt(t) for t in hits)
        return Signal(
            "HIGH_RISK_JURISDICTION",
            3,
            f"{len(hits)} transaction(s) totalling {total:,.2f} with counterparties in "
            f"high-risk jurisdiction(s): {', '.join(cs)}.",
            [t["txn_id"] for t in hits],
        )
    return None


def detect_profile_deviation(txns: list[dict], customer: dict, days: int) -> Signal | None:
    expected = customer.get("expected_monthly_volume")
    if not expected:
        return None
    months = max(days / 30.0, 1.0)
    actual = sum(_amt(t) for t in txns if t["direction"] == "in") / months
    ratio = actual / float(expected)
    if ratio >= 3:
        return Signal(
            "PROFILE_DEVIATION",
            2 if ratio < 6 else 3,
            f"Average monthly inbound volume {actual:,.0f} is {ratio:.1f}x the expected "
            f"{float(expected):,.0f} declared at onboarding.",
        )
    return None


def detect_round_amounts(txns: list[dict]) -> Signal | None:
    big = [t for t in txns if _amt(t) >= 1_000]
    rounds = [t for t in big if _amt(t) % 1_000 == 0]
    if len(rounds) >= 5 and len(rounds) / max(len(big), 1) >= 0.5:
        return Signal(
            "ROUND_AMOUNTS",
            1,
            f"{len(rounds)} of {len(big)} transactions ≥1,000 are exact round thousands.",
            [t["txn_id"] for t in rounds],
        )
    return None


def assess(customer: dict, txns: list[dict]) -> tuple[list[dict], int]:
    """Return (signals, rule_score 0-100)."""
    s = get_settings()
    signals: list[Signal] = []
    for sig in (
        detect_structuring(txns, s.structuring_threshold),
        detect_rapid_movement(txns),
        detect_high_risk_jurisdiction(txns, s.high_risk_country_set),
        detect_profile_deviation(txns, customer, s.history_days),
        detect_round_amounts(txns),
    ):
        if sig:
            signals.append(sig)
    if customer.get("pep"):
        signals.append(Signal("PEP", 2, "Customer is a politically exposed person."))
    if (customer.get("risk_rating") or "").lower() == "high":
        signals.append(Signal("HIGH_RISK_CUSTOMER", 2, "Customer risk rating is HIGH."))

    score = min(100, sum(SEVERITY_POINTS[x.severity] for x in signals))
    return [x.dict() for x in signals], score
