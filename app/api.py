"""FastAPI service exposing the triage agent."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from . import db
from .config import get_settings
from .graph import get_graph
from .knowledge import ingest_document


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_schema()
    get_graph()  # creates checkpoint tables on first start
    yield


app = FastAPI(title="AML Alert Triage Agent", version="0.1.0", lifespan=lifespan)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = get_settings().api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class ReviewInput(BaseModel):
    decision: Literal["approve", "override"]
    analyst: str = Field(min_length=1)
    comment: str = Field(min_length=1, description="Rationale is required for the audit trail")
    disposition: Literal["escalate", "close"] | None = None
    sar_narrative: str | None = None


class KnowledgeInput(BaseModel):
    source: str
    kind: Literal["typology", "policy", "regulation"] = "typology"
    content: str = Field(min_length=20, description="Markdown; split into chunks on ## headings")


def _case_view(thread_id: str) -> dict:
    snap = get_graph().get_state({"configurable": {"thread_id": thread_id}})
    if not snap.values:
        raise HTTPException(404, f"No case with thread_id {thread_id}")
    v = snap.values
    pending = [i.value for t in snap.tasks for i in t.interrupts]
    return {
        "thread_id": thread_id,
        "alert_id": v.get("alert_id"),
        "status": "pending_review" if pending else v.get("status"),
        "rule_score": v.get("rule_score"),
        "signals": v.get("signals"),
        "decision": v.get("decision"),
        "guardrail_override": v.get("guardrail_override", False),
        "sar_narrative": v.get("sar_narrative"),
        "retrieved_knowledge": [k["title"] for k in v.get("knowledge", [])],
        "similar_cases": v.get("similar_cases", []),
        "review": v.get("review"),
        "final_disposition": v.get("final_disposition"),
        "pending_review": pending[0] if pending else None,
    }


@app.get("/health")
def health() -> dict:
    with db.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "llm_provider": get_settings().llm_provider}


@app.get("/alerts", dependencies=[Depends(require_api_key)])
def list_alerts(status: str | None = None) -> list[dict]:
    return db.list_alerts(status)


@app.post("/alerts/{alert_id}/triage", dependencies=[Depends(require_api_key)])
def triage(alert_id: str) -> dict:
    alert = db.fetch_alert(alert_id)
    if alert is None:
        raise HTTPException(404, f"Alert {alert_id} not found")
    if alert["status"] in ("escalated", "closed"):
        raise HTTPException(409, f"Alert {alert_id} is already {alert['status']}")
    thread_id = f"{alert_id}:{uuid.uuid4().hex[:8]}"
    get_graph().invoke({"alert_id": alert_id}, {"configurable": {"thread_id": thread_id}})
    return _case_view(thread_id)


@app.get("/cases/{thread_id}", dependencies=[Depends(require_api_key)])
def get_case(thread_id: str) -> dict:
    return _case_view(thread_id)


@app.post("/cases/{thread_id}/review", dependencies=[Depends(require_api_key)])
def review_case(thread_id: str, body: ReviewInput) -> dict:
    view = _case_view(thread_id)
    if view["pending_review"] is None:
        raise HTTPException(409, "Case is not awaiting review")
    if body.decision == "override" and body.disposition is None:
        raise HTTPException(422, "disposition is required when overriding")
    get_graph().invoke(Command(resume=body.model_dump()), {"configurable": {"thread_id": thread_id}})
    return _case_view(thread_id)


@app.get("/alerts/{alert_id}/audit", dependencies=[Depends(require_api_key)])
def audit_trail(alert_id: str) -> list[dict]:
    return db.fetch_audit(alert_id)


@app.post("/knowledge", dependencies=[Depends(require_api_key)])
def add_knowledge(body: KnowledgeInput) -> dict:
    n = ingest_document(body.source, body.kind, body.content)
    return {"source": body.source, "chunks": n}
