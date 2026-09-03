import pytest
import jwt
from datetime import datetime
from fastapi.testclient import TestClient

from services.mcp_server.main import create_order, execute_payment, fetch_payment_status, get_db
from services.mcp_server.db import DBOrder, DBPayment, DBMandate
from services.merchant_app.main import webhook_events_log
from shared.auth.jwt_utils import JWT_SECRET, JWT_ALGORITHM

def test_e2e_happy_path():
    # 1. Fetch token from the MCP Server /token endpoint
    from services.mcp_server.main import app as mcp_app
    mcp_client = TestClient(mcp_app)
    
    token_response = mcp_client.post("/token", json={"session_id": "session_happy_123"})
    assert token_response.status_code == 200
    token = token_response.json()["access_token"]
    
    # 2. Invoke the create_order tool
    order_res = create_order(amount=450.0, token=token, receipt_id="rec_999")
    assert "order_id" in order_res
    assert order_res["amount"] == 450.0
    assert order_res["status"] == "created"
    order_id = order_res["order_id"]
    
    # Check order was saved in database
    db = next(get_db())
    db_order = db.query(DBOrder).filter(DBOrder.order_id == order_id).first()
    assert db_order is not None
    assert db_order.amount == 450.0
    assert db_order.status == "created"
    db.close()
    
    # 3. Invoke execute_payment tool
    idem_key = "idemp_happy_path_123"
    payment_res = execute_payment(
        order_id=order_id,
        payment_method="upi",
        idempotency_key=idem_key,
        token=token
    )
    
    assert "payment_id" in payment_res
    assert payment_res["order_id"] == order_id
    assert payment_res["status"] == "captured"
    assert payment_res["utr_number"] is not None
    assert payment_res["utr_number"].startswith("UTR")
    payment_id = payment_res["payment_id"]
    
    # 4. Check status updates in DB
    db = next(get_db())
    db_order = db.query(DBOrder).filter(DBOrder.order_id == order_id).first()
    assert db_order.status == "paid"
    
    db_payment = db.query(DBPayment).filter(DBPayment.payment_id == payment_id).first()
    assert db_payment is not None
    assert db_payment.status == "captured"
    assert db_payment.utr_number == payment_res["utr_number"]
    db.close()
    
    # 5. Check fetch_payment_status tool
    status_res = fetch_payment_status(payment_id=payment_id, token=token)
    assert status_res["status"] == "captured"
    assert status_res["utr_number"] == payment_res["utr_number"]
    
    # 6. Verify that the webhook was dispatched and received by the Merchant App
    # The dispatcher runs in background tasks, so we wait up to 2 seconds for delivery
    import time
    for _ in range(20):
        if len(webhook_events_log) > 0:
            break
        time.sleep(0.1)
        
    assert len(webhook_events_log) > 0
    latest_event = webhook_events_log[-1]
    assert latest_event["event_type"] == "payment.captured"
    assert latest_event["payload"]["payment_id"] == payment_id
    assert latest_event["payload"]["status"] == "captured"
