"""Postgres + pgvector access: connection pools, schema, and small query helpers."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta
from functools import lru_cache
from typing import Any, Iterator

from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS customers (
    customer_id              TEXT PRIMARY KEY,
    name                     TEXT NOT NULL,
    customer_type            TEXT NOT NULL,          -- individual | business
    country                  TEXT NOT NULL,
    occupation               TEXT,
    risk_rating              TEXT NOT NULL DEFAULT 'medium',
    pep                      BOOLEAN NOT NULL DEFAULT FALSE,
    onboarded_at             DATE,
    expected_monthly_volume  NUMERIC(18,2)       -- expected monthly inbound (credit) volume
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id               TEXT PRIMARY KEY,
    customer_id          TEXT NOT NULL REFERENCES customers(customer_id),
    ts                   TIMESTAMPTZ NOT NULL,
    amount               NUMERIC(18,2) NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'USD',
    direction            TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    channel              TEXT NOT NULL,              -- cash | wire | ach | card
    counterparty         TEXT,
    counterparty_country TEXT,
    description          TEXT
);
CREATE INDEX IF NOT EXISTS ix_txn_customer_ts ON transactions (customer_id, ts);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id            TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL REFERENCES customers(customer_id),
    rule_code           TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    status              TEXT NOT NULL DEFAULT 'open',  -- open | in_review | escalated | closed
    triggering_txn_ids  TEXT[] NOT NULL DEFAULT '{{}}'
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id         BIGSERIAL PRIMARY KEY,
    source     TEXT NOT NULL,
    kind       TEXT NOT NULL,                        -- typology | policy | regulation
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    embedding  vector({dim}) NOT NULL,
    UNIQUE (source, title)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_embedding
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS case_decisions (
    id                 BIGSERIAL PRIMARY KEY,
    alert_id           TEXT NOT NULL REFERENCES alerts(alert_id),
    thread_id          TEXT NOT NULL,
    agent_disposition  TEXT NOT NULL,
    final_disposition  TEXT NOT NULL,
    risk_score         INT NOT NULL,
    summary            TEXT NOT NULL,
    sar_narrative      TEXT,
    analyst            TEXT,
    analyst_comment    TEXT,
    decided_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding          vector({dim}) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_case_embedding
    ON case_decisions USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    alert_id   TEXT,
    thread_id  TEXT,
    step       TEXT NOT NULL,
    detail     JSONB NOT NULL DEFAULT '{{}}',
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_alert ON audit_log (alert_id, at);
"""


def _configure(conn: Connection) -> None:
    register_vector(conn)


@lru_cache
def get_pool() -> ConnectionPool:
    """Pool for application queries (dict rows, pgvector types registered)."""
    # Make sure the extension exists before connections try to register the type.
    import psycopg

    with psycopg.connect(get_settings().dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return ConnectionPool(
        get_settings().dsn,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        configure=_configure,
        open=True,
    )


@lru_cache
def get_checkpoint_pool() -> ConnectionPool:
    """Separate pool for the LangGraph Postgres checkpointer (needs autocommit)."""
    return ConnectionPool(
        get_settings().dsn,
        min_size=1,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )


@contextmanager
def connection() -> Iterator[Connection]:
    with get_pool().connection() as conn:
        yield conn


def init_schema() -> None:
    with connection() as conn:
        conn.execute(SCHEMA_SQL.format(dim=get_settings().embedding_dim))


# --- Query helpers ------------------------------------------------------

def fetch_alert(alert_id: str) -> dict | None:
    with connection() as conn:
        return conn.execute("SELECT * FROM alerts WHERE alert_id = %s", (alert_id,)).fetchone()


def list_alerts(status: str | None = None) -> list[dict]:
    with connection() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM alerts WHERE status = %s ORDER BY created_at", (status,)
            ).fetchall()
        return conn.execute("SELECT * FROM alerts ORDER BY created_at").fetchall()


def fetch_customer(customer_id: str) -> dict | None:
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM customers WHERE customer_id = %s", (customer_id,)
        ).fetchone()


def fetch_transactions(customer_id: str, until, days: int) -> list[dict]:
    with connection() as conn:
        return conn.execute(
            """SELECT * FROM transactions
               WHERE customer_id = %s AND ts > %s AND ts <= %s
               ORDER BY ts""",
            (customer_id, until - timedelta(days=days), until),
        ).fetchall()


def set_alert_status(alert_id: str, status: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE alerts SET status = %s WHERE alert_id = %s", (status, alert_id))


def audit(alert_id: str | None, thread_id: str | None, step: str, detail: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO audit_log (alert_id, thread_id, step, detail) VALUES (%s, %s, %s, %s)",
            (alert_id, thread_id, step, json.dumps(detail, default=str)),
        )


def fetch_audit(alert_id: str) -> list[dict]:
    with connection() as conn:
        return conn.execute(
            "SELECT step, detail, at, thread_id FROM audit_log WHERE alert_id = %s ORDER BY id",
            (alert_id,),
        ).fetchall()
