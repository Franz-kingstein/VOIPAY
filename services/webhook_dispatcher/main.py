import sys
import os
import logging
import hmac
import hashlib
import time
import uuid
import json
import httpx
import asyncio
from typing import Dict, Any, List
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from shared.tracing import TracingMiddleware, get_trace_id

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

class TraceLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[TraceID: {get_trace_id()}] {msg}", kwargs

logger = TraceLoggerAdapter(logging.getLogger("webhook_dispatcher"), {})

app = FastAPI(title="Webhook Dispatcher")
app.add_middleware(TracingMiddleware)

# Webhook Secret Configuration
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "razorpay_webhook_secret_key_2026")

# Target endpoints to notify
TARGET_ENDPOINTS = [
    os.getenv("MERCHANT_WEBHOOK_URL", "http://merchant_app:8080/webhooks/payment"),
    os.getenv("AGENT_INTERNAL_WEBHOOK_URL", "http://agent_core:8001/webhooks/internal")
]

class DispatchRequest(BaseModel):
    event_type: str
    payload: Dict[str, Any]
    trace_id: str

@app.get("/health")
def health():
    return {"status": "ok", "service": "webhook_dispatcher"}

async def post_webhook_with_retry(
    url: str,
    serialized_packet: str,
    signature: str,
    trace_id: str,
    max_retries: int = 3
):
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "X-Trace-ID": trace_id
    }
    
    # Exponential backoff parameters
    delay = 1.0
    
    for attempt in range(1, max_retries + 2):
        try:
            logger.info(f"Dispatching webhook to {url} (Attempt {attempt}/{max_retries + 1})")
            
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, content=serialized_packet, headers=headers)
                
                if resp.status_code >= 200 and resp.status_code < 300:
                    logger.info(f"Successfully dispatched webhook to {url}. Status: {resp.status_code}")
                    return
                else:
                    logger.warning(
                        f"Webhook delivery to {url} failed with status {resp.status_code}. "
                        f"Response: {resp.text[:200]}"
                    )
        except httpx.RequestError as e:
            logger.error(f"Network error delivering webhook to {url} on attempt {attempt}: {e}")
            
        if attempt <= max_retries:
            logger.info(f"Retrying webhook delivery to {url} in {delay} seconds...")
            await asyncio.sleep(delay)
            delay *= 2.0  # exponential factor
            
    logger.error(f"Failed to deliver webhook to {url} after {max_retries + 1} attempts. Giving up.")

@app.post("/dispatch")
def dispatch_webhook(req: DispatchRequest, background_tasks: BackgroundTasks):
    trace_id = req.trace_id or get_trace_id()
    logger.info(f"Request received to dispatch event: {req.event_type} (trace_id={trace_id})")
    
    # Formulate event packet
    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    event_packet = {
        "event_id": event_id,
        "event_type": req.event_type,
        "payload": req.payload,
        "trace_id": trace_id,
        "timestamp": time.time()
    }
    
    # Compute HMAC-SHA256 signature over event packet JSON string
    serialized_packet = json.dumps(event_packet, sort_keys=True)
    signature = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        serialized_packet.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    # Queue delivery in background
    for endpoint in TARGET_ENDPOINTS:
        background_tasks.add_task(
            post_webhook_with_retry,
            url=endpoint,
            serialized_packet=serialized_packet,
            signature=signature,
            trace_id=trace_id
        )
        
    # NEW — additive, isolated failure domain:
    ledger_url = os.getenv("LEDGER_WEBHOOK_URL", "http://ledger_service:8089/webhooks/ledger")
    try:
        background_tasks.add_task(
            post_webhook_with_retry,
            url=ledger_url,
            serialized_packet=serialized_packet,
            signature=signature,
            trace_id=trace_id
        )
    except Exception as e:
        logger.warning(
            "ledger_service webhook scheduling failed",
            extra={"trace_id": trace_id, "error": str(e)}
        )
        
    return {"status": "dispatched", "event_id": event_id}
