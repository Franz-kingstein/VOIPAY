import os
import sys
import json
import hmac
import hashlib
import time
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, Session, select

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from services.ledger_service.main import app, get_db
from services.ledger_service.models import ExpenseEntry, Category, CategoryNature

# Setup in-memory SQLite engine for tests using StaticPool to share connection
sqlite_url = "sqlite:///:memory:"
engine = create_engine(
    sqlite_url,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)

@event.listens_for(engine, "connect")
def connect(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("ATTACH DATABASE ':memory:' AS ledger;")
    cursor.close()

@pytest.fixture(name="db_session")
def session_fixture():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        # Seed categories required
        groceries = Category(name="Groceries", nature=CategoryNature.NEED)
        dining = Category(name="Dining Out", nature=CategoryNature.WANT)
        other = Category(name="Other", nature=CategoryNature.NEED)
        session.add_all([groceries, dining, other])
        session.commit()
    with Session(engine) as session:
        yield session
    SQLModel.metadata.drop_all(engine)

@pytest.fixture(name="client")
def client_fixture(db_session):
    def get_db_override():
        return db_session
    app.dependency_overrides[get_db] = get_db_override
    yield TestClient(app)
    app.dependency_overrides.clear()

def test_dedup_idempotent_ingest(client, db_session):
    WEBHOOK_SECRET = "razorpay_webhook_secret_key_2026"
    event_packet = {
        "event_id": "evt_dup123",
        "event_type": "payment.captured",
        "payload": {
            "payment_id": "pay_dup",
            "order_id": "order_dup",
            "amount": 100.0,
            "payee_name": "Blinkit Groceries",
            "created_at": "2026-08-30T12:00:00Z"
        },
        "trace_id": "trace_dup123",
        "timestamp": time.time()
    }
    
    serialized = json.dumps(event_packet, sort_keys=True)
    signature = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        serialized.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        "X-Razorpay-Signature": signature,
        "Content-Type": "application/json"
    }
    
    # Send first time
    response1 = client.post("/webhooks/ledger", content=serialized, headers=headers)
    assert response1.status_code == 200
    assert response1.json()["status"] == "success"
    
    # Send second time (duplicate)
    response2 = client.post("/webhooks/ledger", content=serialized, headers=headers)
    assert response2.status_code == 200
    assert response2.json()["status"] == "duplicate"
    
    # Assert only ONE row exists
    db_session.expire_all()
    entries = db_session.exec(select(ExpenseEntry).where(ExpenseEntry.payment_id == "pay_dup")).all()
    assert len(entries) == 1
    assert entries[0].amount == 100.0
