import pytest
from fastapi.testclient import TestClient
from services.mcp_server.main import create_order, execute_payment, get_db

from unittest.mock import patch

def test_risk_velocity_decline():
    from services.mcp_server.main import app as mcp_app
    mcp_client = TestClient(mcp_app)
    
    # Use a specific session to check velocity
    token_response = mcp_client.post("/token", json={"session_id": "session_velocity_test"})
    token = token_response.json()["access_token"]
    
    # Create 6 distinct orders
    order_ids = []
    for i in range(6):
        order_res = create_order(amount=10.0, token=token, receipt_id=f"rec_vel_{i}")
        order_ids.append(order_res["order_id"])
        
    async def mock_process_debit(amount, payee):
        import random
        return {
            "status": "captured",
            "reason": "NPCI: Transaction approved",
            "utr_number": f"UTR{random.randint(100000000000, 999999999999)}"
        }
        
    with patch("services.bank_simulator.npci_switch.NPCISwitch.process_debit", side_effect=mock_process_debit):
        # Execute the first 5 payments - these should succeed
        for i in range(5):
            pay_res = execute_payment(
                order_id=order_ids[i],
                payment_method="upi",
                idempotency_key=f"idem_vel_key_{i}",
                token=token
            )
            assert pay_res["status"] == "captured"
            
        # The 6th payment execution should trigger the velocity rule (> 5 txns/min) and be declined
        failed_pay_res = execute_payment(
            order_id=order_ids[5],
            payment_method="upi",
            idempotency_key="idem_vel_key_5",
            token=token
        )
        
        assert failed_pay_res["status"] == "failed"
        assert failed_pay_res["utr_number"] is None
        reasons_str = "".join(failed_pay_res.get("reasons", []))
        assert "VELOCITY_EXCEEDED" in reasons_str

