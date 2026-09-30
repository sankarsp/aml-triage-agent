"""The LLM 'analyst': Bedrock-backed in production, deterministic mock offline."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .config import get_settings

# --- Structured outputs -------------------------------------------------


class TriageDecision(BaseModel):
    disposition: Literal["escalate", "close"] = Field(
        description="'escalate' = refer for SAR consideration; 'close' = no suspicious activity"
    )
    risk_score: int = Field(ge=0, le=100, description="Overall residual risk, 0-100")
    summary: str = Field(description="3-6 sentence investigation summary citing evidence")
    key_findings: list[str] = Field(description="Specific, evidence-backed findings")
    matched_typologies: list[str] = Field(
        default_factory=list, description="Typology titles from the knowledge base that apply"
    )
    recommended_actions: list[str] = Field(default_factory=list)


class SarNarrative(BaseModel):
    narrative: str = Field(description="Draft SAR narrative (who/what/when/where/why/how)")


SYSTEM_PROMPT = """You are a senior AML investigator at a regulated payments company.
You triage transaction-monitoring alerts. Rules:
- Base every finding on the evidence provided (customer profile, transactions, rule
  signals, retrieved typologies/policy, similar past cases). Never invent facts,
  amounts, dates or counterparties.
- Cite transaction IDs for each finding.
- Recommend 'escalate' when activity is unexplained by the customer's profile and matches
  a known typology; recommend 'close' only when there is a plausible legitimate
  explanation supported by the data, and say what that explanation is.
- You recommend; a human analyst decides. Do not state that a SAR has been filed.
"""

SAR_PROMPT = """Draft a SAR narrative for analyst review following the institution's
narrative standards in the retrieved policy. Cover who, what, when, where, why it is
suspicious, and how the activity was conducted. Use only facts from the case file.
Plain prose, no headings, under 450 words."""


class Analyst(Protocol):
    def decide(self, case: dict) -> TriageDecision: ...
    def draft_sar(self, case: dict) -> str: ...


def render_case(case: dict, max_txns: int = 60) -> str:
    """Compact, model-friendly rendering of the case file."""
    c = case["customer"]
    txns = case["transactions"]
    shown = txns[-max_txns:]
    lines = [
        f"ALERT {case['alert']['alert_id']} rule={case['alert']['rule_code']} "
        f"created={case['alert']['created_at']:%Y-%m-%d}",
        "",
        "CUSTOMER: " + json.dumps(
            {k: c[k] for k in ("customer_id", "name", "customer_type", "country", "occupation",
                               "risk_rating", "pep", "expected_monthly_volume")},
            default=str,
        ),
        "",
        f"TRANSACTIONS (last {len(shown)} of {len(txns)} in lookback window):",
        "txn_id | date | dir | channel | amount | counterparty | cp_country | description",
    ]
    for t in shown:
        lines.append(
            f"{t['txn_id']} | {t['ts']:%Y-%m-%d} | {t['direction']} | {t['channel']} | "
            f"{float(t['amount']):,.2f} {t['currency']} | {t.get('counterparty') or '-'} | "
            f"{t.get('counterparty_country') or '-'} | {t.get('description') or ''}"
        )
    lines += ["", f"RULE SIGNALS (rule score {case['rule_score']}):"]
    lines += [f"- [{s['code']}] sev={s['severity']}: {s['description']} "
              f"evidence={s['evidence_txn_ids']}" for s in case["signals"]] or ["- none"]
    lines += ["", "RETRIEVED KNOWLEDGE:"]
    lines += [f"--- {k['title']} ({k['kind']}, sim={k['similarity']})\n{k['content']}"
              for k in case["knowledge"]] or ["- none"]
    lines += ["", "SIMILAR PAST CASES (analyst-reviewed):"]
    lines += [f"- {p['alert_id']}: {p['final_disposition']} score={p['risk_score']} "
              f"sim={p['similarity']} :: {p['summary'][:300]} | analyst: {p.get('analyst_comment') or ''}"
              for p in case["similar_cases"]] or ["- none"]
    return "\n".join(lines)


class BedrockAnalyst:
    def __init__(self) -> None:
        from langchain_aws import ChatBedrockConverse

        s = get_settings()
        self.llm = ChatBedrockConverse(
            model=s.bedrock_model_id, region_name=s.aws_region, temperature=0, max_tokens=2500
        )

    def decide(self, case: dict) -> TriageDecision:
        return self.llm.with_structured_output(TriageDecision).invoke(
            [("system", SYSTEM_PROMPT), ("human", render_case(case) + "\n\nTriage this alert.")]
        )

    def draft_sar(self, case: dict) -> str:
        decision = json.dumps(case["decision"], indent=1)
        out = self.llm.with_structured_output(SarNarrative).invoke(
            [("system", SYSTEM_PROMPT),
             ("human", f"{render_case(case)}\n\nTRIAGE DECISION:\n{decision}\n\n{SAR_PROMPT}")]
        )
        return out.narrative


class MockAnalyst:
    """Deterministic stand-in so the full graph runs offline and in CI."""

    def decide(self, case: dict) -> TriageDecision:
        sigs = case["signals"]
        score = case["rule_score"]
        high = [s for s in sigs if s["severity"] >= 3]
        escalate = bool(high) or score >= 50
        findings = [f"{s['description']} (txns: {', '.join(s['evidence_txn_ids'][:6]) or 'n/a'})"
                    for s in sigs] or ["No rule-based risk signals in the lookback window."]
        c = case["customer"]
        if escalate:
            summary = (f"{c['name']} ({c['occupation']}) shows {len(sigs)} risk signal(s), "
                       f"including {', '.join(s['code'] for s in high) or 'elevated activity'}. "
                       "The activity is not explained by the declared profile. Referral for SAR "
                       "consideration is recommended.")
        else:
            summary = (f"Activity for {c['name']} ({c['occupation']}) is consistent with the "
                       "declared business profile; the alerted transaction has a plausible "
                       "legitimate explanation. Recommend closing with no further action.")
        return TriageDecision(
            disposition="escalate" if escalate else "close",
            risk_score=max(score, 15),
            summary=summary,
            key_findings=findings,
            matched_typologies=[k["title"] for k in case["knowledge"][:2]] if escalate else [],
            recommended_actions=(["Refer to FIU for SAR decision", "Request source-of-funds documentation"]
                                 if escalate else ["Close alert", "Retain rationale in case file"]),
        )

    def draft_sar(self, case: dict) -> str:
        c, d = case["customer"], case["decision"]
        txns = {t["txn_id"]: t for t in case["transactions"]}
        ev = sorted({i for s in case["signals"] for i in s["evidence_txn_ids"]})
        dated = [txns[i] for i in ev if i in txns]
        span = (f"Between {min(t['ts'] for t in dated):%B %d, %Y} and {max(t['ts'] for t in dated):%B %d, %Y}"
                if dated else "During the review period")
        total = sum(float(t["amount"]) for t in dated)
        return (
            f"This report concerns {c['name']} (customer {c['customer_id']}), "
            f"{'an' if c['customer_type'][0] in 'aeiou' else 'a'} {c['customer_type']} customer located in {c['country']} whose stated occupation or "
            f"business is {c['occupation']}. {span}, the customer conducted "
            f"{len(dated)} transactions totalling {total:,.2f} USD that are the subject of this "
            f"report (transaction IDs: {', '.join(ev)}). "
            + " ".join(d["key_findings"])
            + " The activity is inconsistent with the customer's expected monthly volume of "
            f"{float(c['expected_monthly_volume'] or 0):,.0f} USD and with its stated purpose. "
            "No documentation has been obtained that explains the source or purpose of the funds. "
            "[DRAFT generated for analyst review — verify all facts before filing.]"
        )


@lru_cache
def get_analyst() -> Analyst:
    return MockAnalyst() if get_settings().llm_provider == "mock" else BedrockAnalyst()
