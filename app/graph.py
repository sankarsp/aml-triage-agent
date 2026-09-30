"""LangGraph workflow for AML alert triage.

    load_case → assess_risk → retrieve_context → analyze ─┬─ escalate ─→ draft_sar ─┐
                                                           └─ close ──────────────────┤
                                                                                      ▼
                                                             human_review (interrupt) → finalize

Every node writes to the audit log. The graph pauses at human_review until an
analyst approves / overrides via the API, so no disposition is final without a
person signing off (unless AUTO_CLOSE_MAX_SCORE explicitly allows it).
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import db, rules
from .analyst import get_analyst
from .config import get_settings
from .knowledge import embed_text, search_knowledge, similar_cases


class TriageState(TypedDict, total=False):
    alert_id: str
    alert: dict
    customer: dict
    transactions: list[dict]
    signals: list[dict]
    rule_score: int
    knowledge: list[dict]
    similar_cases: list[dict]
    decision: dict
    guardrail_override: bool
    sar_narrative: str | None
    review: dict
    final_disposition: str
    status: str


def _plain(row: dict) -> dict:
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


def _thread(config: RunnableConfig) -> str:
    return config["configurable"]["thread_id"]


# --- Nodes ----------------------------------------------------------------

def load_case(state: TriageState, config: RunnableConfig) -> dict:
    alert = db.fetch_alert(state["alert_id"])
    if alert is None:
        raise ValueError(f"Alert {state['alert_id']} not found")
    customer = db.fetch_customer(alert["customer_id"])
    txns = db.fetch_transactions(alert["customer_id"], alert["created_at"], get_settings().history_days)
    db.set_alert_status(alert["alert_id"], "in_review")
    db.audit(alert["alert_id"], _thread(config), "load_case",
             {"transactions": len(txns), "rule_code": alert["rule_code"]})
    return {"alert": _plain(alert), "customer": _plain(customer),
            "transactions": [_plain(t) for t in txns], "status": "investigating"}


def assess_risk(state: TriageState, config: RunnableConfig) -> dict:
    signals, score = rules.assess(state["customer"], state["transactions"])
    db.audit(state["alert_id"], _thread(config), "assess_risk",
             {"rule_score": score, "signals": [s["code"] for s in signals]})
    return {"signals": signals, "rule_score": score}


def _case_query(state: TriageState) -> str:
    c = state["customer"]
    sig = "; ".join(s["description"] for s in state["signals"]) or "no rule signals"
    return (f"Alert {state['alert']['rule_code']} for {c['customer_type']} customer, "
            f"occupation {c['occupation']}, country {c['country']}. Signals: {sig}")


def retrieve_context(state: TriageState, config: RunnableConfig) -> dict:
    q = _case_query(state)
    kb = search_knowledge(q, k=4)
    past = similar_cases(q, k=3, exclude_alert=state["alert_id"])
    db.audit(state["alert_id"], _thread(config), "retrieve_context",
             {"knowledge": [k["title"] for k in kb], "similar_cases": [p["alert_id"] for p in past]})
    return {"knowledge": kb, "similar_cases": past}


def analyze(state: TriageState, config: RunnableConfig) -> dict:
    decision = get_analyst().decide(dict(state)).model_dump()
    override = False
    if decision["disposition"] == "close" and state["rule_score"] >= get_settings().force_escalate_score:
        override = True
        decision["disposition"] = "escalate"
        decision["risk_score"] = max(decision["risk_score"], state["rule_score"])
        decision["key_findings"].append(
            f"Guardrail: rule score {state['rule_score']} ≥ {get_settings().force_escalate_score}; "
            "model recommendation to close was overridden to escalate for human decision.")
    db.audit(state["alert_id"], _thread(config), "analyze",
             {"disposition": decision["disposition"], "risk_score": decision["risk_score"],
              "guardrail_override": override})
    return {"decision": decision, "guardrail_override": override}


def draft_sar(state: TriageState, config: RunnableConfig) -> dict:
    narrative = get_analyst().draft_sar(dict(state))
    db.audit(state["alert_id"], _thread(config), "draft_sar", {"chars": len(narrative)})
    return {"sar_narrative": narrative}


def human_review(state: TriageState, config: RunnableConfig) -> dict:
    d = state["decision"]
    s = get_settings()
    if d["disposition"] == "close" and d["risk_score"] <= s.auto_close_max_score:
        review = {"decision": "approve", "analyst": "system:auto-close",
                  "comment": f"Auto-closed: score {d['risk_score']} ≤ {s.auto_close_max_score}"}
    else:
        # Pauses the graph; the API resumes it with Command(resume=<analyst input>).
        # (On resume LangGraph re-runs this node and interrupt() returns the input,
        # so anything with side effects belongs after this call.)
        review = interrupt({
            "alert_id": state["alert_id"],
            "recommendation": d["disposition"],
            "risk_score": d["risk_score"],
            "summary": d["summary"],
            "key_findings": d["key_findings"],
            "sar_narrative": state.get("sar_narrative"),
            "instructions": "Respond with decision=approve|override, analyst, comment, "
                            "and optionally disposition (for override) and edited sar_narrative.",
        })
    db.audit(state["alert_id"], _thread(config), "human_review",
             {"analyst": review.get("analyst"), "decision": review.get("decision"),
              "comment": review.get("comment")})
    return {"review": review}


def finalize(state: TriageState, config: RunnableConfig) -> dict:
    d, r = state["decision"], state["review"]
    final = d["disposition"]
    if r.get("decision") == "override":
        final = r.get("disposition") or ("close" if final == "escalate" else "escalate")
    narrative = r.get("sar_narrative") or state.get("sar_narrative")
    if final != "escalate":
        narrative = None
    # Store the reviewed decision as a vector so future cases can learn from it.
    text = f"{_case_query(state)}\nOutcome: {final}. {d['summary']} {r.get('comment') or ''}"
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO case_decisions (alert_id, thread_id, agent_disposition, final_disposition,
                   risk_score, summary, sar_narrative, analyst, analyst_comment, embedding)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (state["alert_id"], _thread(config), d["disposition"], final, d["risk_score"],
             d["summary"], narrative, r.get("analyst"), r.get("comment"), embed_text(text)),
        )
    status = "escalated" if final == "escalate" else "closed"
    db.set_alert_status(state["alert_id"], status)
    db.audit(state["alert_id"], _thread(config), "finalize",
             {"agent": d["disposition"], "final": final, "analyst": r.get("analyst"),
              "review_decision": r.get("decision")})
    return {"final_disposition": final, "sar_narrative": narrative, "status": status}


def route_after_analysis(state: TriageState) -> Literal["draft_sar", "human_review"]:
    return "draft_sar" if state["decision"]["disposition"] == "escalate" else "human_review"


# --- Graph assembly ---------------------------------------------------------

def build_graph(checkpointer: Any = None):
    g = StateGraph(TriageState)
    g.add_node("load_case", load_case)
    g.add_node("assess_risk", assess_risk)
    g.add_node("retrieve_context", retrieve_context)
    g.add_node("analyze", analyze)
    g.add_node("draft_sar", draft_sar)
    g.add_node("human_review", human_review)
    g.add_node("finalize", finalize)

    g.add_edge(START, "load_case")
    g.add_edge("load_case", "assess_risk")
    g.add_edge("assess_risk", "retrieve_context")
    g.add_edge("retrieve_context", "analyze")
    g.add_conditional_edges("analyze", route_after_analysis)
    g.add_edge("draft_sar", "human_review")
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


@lru_cache
def get_graph():
    saver = PostgresSaver(db.get_checkpoint_pool())
    saver.setup()
    return build_graph(saver)
