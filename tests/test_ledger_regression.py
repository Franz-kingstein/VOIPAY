import os
import sys
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
import httpx

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.webhook_dispatcher.main import app

def test_dispatcher_ledger_outage_regression():
    client = TestClient(app)
    
    dispatch_request = {
        "event_type": "payment.captured",
        "payload": {
            "payment_id": "pay_regress_123",
            "order_id": "order_regress_123",
            "amount": 500.0,
            "payee_name": "Test Payee",
            "created_at": "2026-08-30T12:00:00Z"
        },
        "trace_id": "trace_regress_999"
    }
    
    # We patch httpx.AsyncClient.post to simulate ledger_service connection failure,
    # and merchant_app/agent_core success.
    calls = []
    
    async def mock_post(url, *args, **kwargs):
        calls.append(url)
        if "ledger_service" in url:
            # Raise network error for ledger_service
            raise httpx.RequestError("Connection refused by ledger_service", request=None)
        else:
            # Return HTTP 200 OK mock response for others
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_resp.text = "OK"
            return mock_resp
            
    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        # We trigger the dispatcher POST request
        # Note: Background tasks are executed synchronously in the test client after the request returns,
        # so any exceptions raised inside background tasks will propagate or log.
        response = client.post("/dispatch", json=dispatch_request)
        
        # Verify that the dispatcher endpoint returns 200 OK immediately
        assert response.status_code == 200
        assert response.json()["status"] == "dispatched"
        
    # Verify that all targets were attempted, including merchant_app, agent_core, and ledger_service
    assert any("merchant_app" in url for url in calls)
    assert any("agent_core" in url for url in calls)
    assert any("ledger_service" in url for url in calls)
