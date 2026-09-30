from fastapi.testclient import TestClient

from app.api import app


def client():
    return TestClient(app)


def test_structuring_alert_escalates_with_sar_and_human_approval(seeded):
    with client() as c:
        r = c.post("/alerts/A-1001/triage")
        assert r.status_code == 200, r.text
        case = r.json()
        assert case["status"] == "pending_review"
        assert case["decision"]["disposition"] == "escalate"
        assert {s["code"] for s in case["signals"]} >= {"STRUCTURING"}
        assert "Structuring" in " ".join(case["retrieved_knowledge"])
        assert case["sar_narrative"] and "Sunrise Mini Mart" in case["sar_narrative"]

        r = c.post(f"/cases/{case['thread_id']}/review",
                   json={"decision": "approve", "analyst": "jdoe", "comment": "Agree; refer to FIU."})
        assert r.status_code == 200, r.text
        done = r.json()
        assert done["status"] == "escalated" and done["final_disposition"] == "escalate"

        assert c.post("/alerts/A-1001/triage").status_code == 409
        steps = [a["step"] for a in c.get("/alerts/A-1001/audit").json()]
        assert steps[0] == "load_case" and steps[-1] == "finalize"
        assert c.get("/alerts", params={"status": "escalated"}).json()[0]["alert_id"] == "A-1001"


def test_benign_alert_recommends_close_and_analyst_can_override(seeded):
    with client() as c:
        case = c.post("/alerts/A-1003/triage").json()
        assert case["decision"]["disposition"] == "close"
        assert case["sar_narrative"] is None
        # override requires a disposition
        bad = c.post(f"/cases/{case['thread_id']}/review",
                     json={"decision": "override", "analyst": "jdoe", "comment": "x"})
        assert bad.status_code == 422
        done = c.post(f"/cases/{case['thread_id']}/review",
                      json={"decision": "override", "disposition": "close", "analyst": "jdoe",
                            "comment": "Quarterly insurer reconciliation; confirmed with remittance advice."}).json()
        assert done["status"] == "closed"


def test_reviewed_cases_feed_similar_case_retrieval(seeded):
    with client() as c:
        first = c.post("/alerts/A-1001/triage").json()
        c.post(f"/cases/{first['thread_id']}/review",
               json={"decision": "approve", "analyst": "jdoe", "comment": "Classic structuring."})
        second = c.post("/alerts/A-1002/triage").json()
        assert [p["alert_id"] for p in second["similar_cases"]] == ["A-1001"]
        assert second["decision"]["disposition"] == "escalate"


def test_knowledge_ingest_and_unknown_alert(seeded):
    with client() as c:
        r = c.post("/knowledge", json={"source": "tbml.md", "kind": "typology",
                                        "content": "# Typology: TBML\n\n## Red flags\nOver- or under-invoicing of goods relative to market prices."})
        assert r.json() == {"source": "tbml.md", "chunks": 1}
        assert c.post("/alerts/NOPE/triage").status_code == 404
        assert c.get("/health").json()["status"] == "ok"


def test_guardrail_overrides_model_close_on_high_rule_score(seeded, monkeypatch):
    from app import graph
    from app.analyst import MockAnalyst, TriageDecision

    class LenientAnalyst(MockAnalyst):
        def decide(self, case):
            return TriageDecision(disposition="close", risk_score=20, summary="Looks fine.",
                                  key_findings=["Customer explained it."])

    monkeypatch.setattr(graph, "get_analyst", lambda: LenientAnalyst())
    with client() as c:
        case = c.post("/alerts/A-1002/triage").json()
        assert case["guardrail_override"] is True
        assert case["decision"]["disposition"] == "escalate"
        assert case["sar_narrative"]
