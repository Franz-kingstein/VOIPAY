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
from services.ledger_service.main import app, get_db, seed_categories
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
    # Seed categories for classification check
    with Session(engine) as session:
        # Create categories required for testing
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

def test_webhook_ingest_success(client, db_session):
    WEBHOOK_SECRET = "razorpay_webhook_secret_key_2026"
    event_packet = {
        "event_id": "evt_test123",
        "event_type": "payment.captured",
        "payload": {
            "payment_id": "pay_testwebhook",
            "order_id": "order_testwebhook",
            "amount": 250.0,
            "payee_name": "Swiggy Restaurant",
            "created_at": "2026-08-30T12:00:00Z"
        },
        "trace_id": "trace_test123",
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
    
    response = client.post("/webhooks/ledger", content=serialized, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # Assert entry is created in database
    db_session.expire_all()
    entry = db_session.exec(select(ExpenseEntry).where(ExpenseEntry.payment_id == "pay_testwebhook")).first()
    assert entry is not None
    assert entry.amount == 250.0
    assert entry.payee == "Swiggy Restaurant"
    assert entry.nature == CategoryNature.WANT  # Swiggy classified as Dining Out -> WANT
    assert entry.trace_id == "trace_test123"
