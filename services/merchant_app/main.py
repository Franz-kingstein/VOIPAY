import sys
import os
import logging
import hmac
import hashlib
import asyncio
import json
from typing import Dict, Any, List
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

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

logger = TraceLoggerAdapter(logging.getLogger("merchant_app"), {})

app = FastAPI(title="Merchant Dashboard Hub")
app.add_middleware(TracingMiddleware)

# Shared Secret for Webhook Signature Verification
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "razorpay_webhook_secret_key_2026")

# In-memory store for webhook events and active SSE queues
webhook_events_log: List[Dict[str, Any]] = []
sse_listeners: List[asyncio.Queue] = []

@app.get("/health")
def health():
    return {"status": "ok", "service": "merchant_app"}

# Webhook Endpoint (Receives signed transaction results)
@app.post("/webhooks/payment")
async def receive_webhook(request: Request):
    trace_id = request.headers.get("X-Trace-ID") or get_trace_id()
    signature = request.headers.get("X-Razorpay-Signature")
    
    if not signature:
        logger.warning("Rejecting webhook: Missing signature header X-Razorpay-Signature")
        raise HTTPException(status_code=400, detail="Missing signature header")
        
    body_bytes = await request.body()
    
    # Verify HMAC-SHA256 signature
    try:
        # Since dispatcher serializes JSON with sort_keys=True, the exact body bytes can be hashed
        computed = hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            body_bytes,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(computed, signature):
            logger.warning("Rejecting webhook: Signature verification mismatch")
            raise HTTPException(status_code=401, detail="Signature mismatch")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking webhook signature: {e}")
        raise HTTPException(status_code=500, detail="Internal signature parsing error")

    # Load payload
    try:
        event = json.loads(body_bytes.decode("utf-8"))
    except Exception as je:
        logger.error(f"Failed to decode webhook JSON: {je}")
        raise HTTPException(status_code=400, detail="Invalid JSON format")
        
    logger.info(f"Verified webhook received: type={event.get('event_type')}, event_id={event.get('event_id')}")
    
    # Save to history log (keep last 50 entries)
    webhook_events_log.append(event)
    if len(webhook_events_log) > 50:
        webhook_events_log.pop(0)
        
    # Broadcast to active SSE listeners
    sse_message = {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "payload": event.get("payload"),
        "trace_id": event.get("trace_id", trace_id),
        "timestamp": event.get("timestamp")
    }
    
    for queue in sse_listeners:
        await queue.put(sse_message)
        
    return {"status": "processed", "event_id": event.get("event_id")}

# SSE Endpoint to stream real-time events to frontend
@app.get("/events")
async def stream_sse_events(request: Request):
    logger.info("New SSE client subscribed to dashboard events")
    queue = asyncio.Queue()
    sse_listeners.append(queue)
    
    # Replay past events on initial load
    for event in webhook_events_log:
        await queue.put(event)
        
    async def sse_generator():
        try:
            while True:
                # Wait for next event
                event_data = await queue.get()
                yield f"data: {json.dumps(event_data)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("SSE client disconnected from dashboard events")
            sse_listeners.remove(queue)
            
    return StreamingResponse(sse_generator(), media_type="text/event-stream")

import httpx

AGENT_CORE_URL = os.getenv("AGENT_CORE_URL", "http://agent_core:8001")

# Expose history
@app.get("/api/events")
def get_events_history():
    return webhook_events_log

@app.post("/api/events/clear")
def clear_events():
    webhook_events_log.clear()
    return {"status": "cleared"}

@app.post("/api/verify_pin")
async def proxy_verify_pin(payload: dict):
    logger.info(f"Proxying PIN verification payload to Agent Core: {payload}")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"{AGENT_CORE_URL}/agent/verify_pin", json=payload)
            if resp.status_code == 200:
                return resp.json()
            else:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
        except Exception as e:
            logger.error(f"Failed to communicate with Agent Core: {e}")
            raise HTTPException(status_code=500, detail="Agent Core connection failed")

# Serve Frontend static directory
current_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(current_dir, "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
