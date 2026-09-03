import pytest
from fastapi.testclient import TestClient
from services.mcp_server.main import create_order, execute_payment, validate_mandate, get_db

def test_mandate_limit_exceeded():
    from services.mcp_server.main import app as mcp_app
    mcp_client = TestClient(mcp_app)
    
    token_response = mcp_client.post("/token", json={"session_id": "session_mandate_test"})
    token = token_response.json()["access_token"]
    
    # 1. Register a delegated mandate in the Bank Simulator
    from services.bank_simulator.main import app as bank_app
    bank_client = TestClient(bank_app)
    
    mandate_id = "test_mandate_xyz_123"
    reg_response = bank_client.post("/upi/mandate", json={
        "mandate_id": mandate_id,
        "max_amount": 1000.0,  # ₹1000 limit
        "payee_name": "Ramesh",
        "frequency": "monthly"
    })
    assert reg_response.status_code == 200
    
    # 2. Sync and validate mandate via MCP tool
    val_res = validate_mandate(mandate_id=mandate_id, amount=400.0, token=token)
    assert val_res["valid"] is True
    # The validation call itself registers and debits ₹400 from simulator, leaving ₹600
    assert val_res["mandate"]["remaining_limit"] == 600.0
    
    # 3. Create a first order for ₹400
    order_1 = create_order(amount=400.0, token=token, receipt_id="rec_mandate_first")
    order_id_1 = order_1["order_id"]
    
    # Execute payment against mandate
    pay_res_1 = execute_payment(
        order_id=order_id_1,
        payment_method="upi",
        idempotency_key="idem_mandate_pay_1",
        token=token,
        mandate_id=mandate_id
    )
    assert pay_res_1["status"] == "captured"
    
    # Check that remaining limit in simulator has decreased to ₹200 after payment
    # Let's validate the mandate again for a small amount (₹10), which will debit it to ₹190
    val_res_2 = validate_mandate(mandate_id=mandate_id, amount=10.0, token=token)
    assert val_res_2["mandate"]["remaining_limit"] == 190.0
    
    # 4. Create a second order for ₹700 (exceeds remaining ₹600 limit, but less than original max ₹1000)
    order_2 = create_order(amount=700.0, token=token, receipt_id="rec_mandate_second")
    order_id_2 = order_2["order_id"]
    
    # Try to execute payment against mandate - should fail
    pay_res_2 = execute_payment(
        order_id=order_id_2,
        payment_method="upi",
        idempotency_key="idem_mandate_pay_2",
        token=token,
        mandate_id=mandate_id
    )
    
    assert pay_res_2["status"] == "failed"
    assert pay_res_2["utr_number"] is None
    # Verify reason matches mandate limit error
    reasons_str = "".join(pay_res_2.get("reasons", []))
    assert "Mandate limits exceeded" in reasons_str
