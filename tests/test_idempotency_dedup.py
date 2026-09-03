import pytest
from fastapi.testclient import TestClient
from services.mcp_server.main import create_order, execute_payment, get_db

def test_idempotency_dedup():
    from services.mcp_server.main import app as mcp_app
    mcp_client = TestClient(mcp_app)
    
    token_response = mcp_client.post("/token", json={"session_id": "session_idemp_123"})
    token = token_response.json()["access_token"]
    
    # 1. Create order
    order_res = create_order(amount=150.0, token=token, receipt_id="rec_idemp_1")
    order_id = order_res["order_id"]
    
    # 2. Execute payment (First Attempt)
    idem_key = "test_idem_key_999"
    first_pay_res = execute_payment(
        order_id=order_id,
        payment_method="upi",
        idempotency_key=idem_key,
        token=token
    )
    assert first_pay_res["status"] == "captured"
    payment_id = first_pay_res["payment_id"]
    utr = first_pay_res["utr_number"]
    
    # 3. Execute payment (Second Attempt - Duplicate)
    second_pay_res = execute_payment(
        order_id=order_id,
        payment_method="upi",
        idempotency_key=idem_key,
        token=token
    )
    
    # Assert it returns the exact same payment response
    assert second_pay_res["payment_id"] == payment_id
    assert second_pay_res["status"] == "captured"
    assert second_pay_res["utr_number"] == utr
    
    # 4. Create another order
    another_order_res = create_order(amount=200.0, token=token, receipt_id="rec_idemp_2")
    another_order_id = another_order_res["order_id"]
    
    # 5. Try to execute payment for the new order using the SAME idempotency key
    # It must raise an exception indicating payload conflict (HTTP 409 Conflict)
    with pytest.raises(Exception) as exc_info:
        execute_payment(
            order_id=another_order_id,
            payment_method="upi",
            idempotency_key=idem_key,
            token=token
        )
    
    assert "Idempotency key conflict" in str(exc_info.value)
