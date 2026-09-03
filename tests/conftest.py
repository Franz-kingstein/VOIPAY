import pytest
import sys
import os
import httpx
import re
from fastapi.testclient import TestClient

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import all service applications
from services.bank_simulator.main import app as bank_app
from services.risk_engine.main import app as risk_app
from services.idempotency_service.main import app as idempotency_app
from services.webhook_dispatcher.main import app as dispatcher_app
from services.mcp_server.main import app as mcp_app
from services.agent_core.main import app as agent_app
from services.merchant_app.main import app as merchant_app

# Service registry
clients = {
    "bank_simulator": TestClient(bank_app),
    "risk_engine": TestClient(risk_app),
    "idempotency_service": TestClient(idempotency_app),
    "webhook_dispatcher": TestClient(dispatcher_app),
    "mcp_server": TestClient(mcp_app),
    "agent_core": TestClient(agent_app),
    "merchant_app": TestClient(merchant_app),
}

def resolve_mock_route(url: str):
    # Map service hostname or localhost:port to registry
    # E.g., http://bank_simulator:8003/npci/pay -> bank_simulator, /npci/pay
    # E.g., http://localhost:8003/npci/pay -> bank_simulator, /npci/pay
    
    url_str = str(url)
    
    mapping = [
        ("bank_simulator", "bank_simulator"),
        ("risk_engine", "risk_engine"),
        ("idempotency", "idempotency_service"),
        ("dispatcher", "webhook_dispatcher"),
        ("mcp_server", "mcp_server"),
        ("agent_core", "agent_core"),
        ("merchant_app", "merchant_app"),
        (":8003", "bank_simulator"),
        (":8004", "risk_engine"),
        (":8005", "idempotency_service"),
        (":8006", "webhook_dispatcher"),
        (":8002", "mcp_server"),
        (":8001", "agent_core"),
        (":8080", "merchant_app"),
    ]
    
    for marker, target in mapping:
        if marker in url_str:
            # Extract path (everything after port/domain)
            match = re.search(r'https?://[^/]+(/.*)?', url_str)
            path = match.group(1) if match and match.group(1) else "/"
            return clients[target], path
            
    return None, None

# Patch sync httpx.Client calls
original_send = httpx.Client.send

def patched_send(self, request, *args, **kwargs):
    client, path = resolve_mock_route(request.url)
    if client:
        # Prepare headers and body for TestClient
        headers = dict(request.headers)
        body = request.read()
        
        # TestClient request
        # Use TestClient's internal request router
        headers_dict = {k: v for k, v in headers.items()}
        method = request.method
        
        # Execute in-memory
        res = client.request(
            method,
            path,
            content=body,
            headers=headers_dict
        )
        
        # Wrap response back into httpx.Response
        return httpx.Response(
            status_code=res.status_code,
            headers=httpx.Headers(res.headers),
            content=res.content,
            request=request
        )
    return original_send(self, request, *args, **kwargs)

httpx.Client.send = patched_send

# Patch async httpx.AsyncClient calls
original_async_send = httpx.AsyncClient.send

async def patched_async_send(self, request, *args, **kwargs):
    client, path = resolve_mock_route(request.url)
    if client:
        # Prepare headers and body for TestClient
        headers = dict(request.headers)
        body = request.read()
        
        # Run synchronous TestClient requests inside async loop thread pool
        import anyio
        headers_dict = {k: v for k, v in headers.items()}
        method = request.method
        
        def run_request():
            return client.request(
                method,
                path,
                content=body,
                headers=headers_dict
            )
            
        res = await anyio.to_thread.run_sync(run_request)
        
        return httpx.Response(
            status_code=res.status_code,
            headers=httpx.Headers(res.headers),
            content=res.content,
            request=request
        )
    return await original_async_send(self, request, *args, **kwargs)

httpx.AsyncClient.send = patched_async_send

@pytest.fixture(autouse=True)
def setup_env():
    # Setup test env overrides
    os.environ["LIVE_MODE"] = "false"
    os.environ["MAX_DEMO_AMOUNT"] = "5000.0"
    os.environ["REDIS_HOST"] = "localhost"
    os.environ["REDIS_PORT"] = "6379"
    os.environ["DATABASE_URL"] = "sqlite:///mock_razorpay.db"
    
    # Clean databases / SQLite files by dropping and recreating tables
    from services.mcp_server.db import Base, engine
    if engine:
        try:
            Base.metadata.drop_all(bind=engine)
            Base.metadata.create_all(bind=engine)
        except Exception as de:
            print(f"Failed to reset DB: {de}")
            
    # Reset in-memory maps in downstream mocks if any
    from services.idempotency_service.main import in_memory_store
    in_memory_store.clear()
    
    from services.risk_engine.rules import session_transaction_times
    session_transaction_times.clear()
