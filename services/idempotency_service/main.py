import sys
import os
import logging
import json
import redis
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, status
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

logger = TraceLoggerAdapter(logging.getLogger("idempotency_service"), {})

app = FastAPI(title="Idempotency & Hash Engine")
app.add_middleware(TracingMiddleware)

# Redis Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))

# Try connecting to Redis, fall back to in-memory dict if unavailable
redis_client = None
in_memory_store: Dict[str, str] = {}

try:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        socket_timeout=2.0,
        decode_responses=True
    )
    redis_client.ping()
    logger.info(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
except Exception as e:
    logger.warning(f"Could not connect to Redis: {e}. Falling back to in-memory storage.")
    redis_client = None

class IdempotencyCheckRequest(BaseModel):
    idempotency_key: str
    payload_hash: str

class IdempotencyCompleteRequest(BaseModel):
    idempotency_key: str
    payload_hash: str
    response: Dict[str, Any]

def get_store(key: str) -> Optional[str]:
    if redis_client:
        try:
            return redis_client.get(key)
        except Exception as e:
            logger.error(f"Redis get error: {e}")
    return in_memory_store.get(key)

def set_store(key: str, value: str, ttl: int = 86400) -> bool:
    if redis_client:
        try:
            redis_client.setex(key, ttl, value)
            return True
        except Exception as e:
            logger.error(f"Redis setex error: {e}")
    in_memory_store[key] = value
    return True

def setnx_store(key: str, value: str, ttl: int = 86400) -> bool:
    if redis_client:
        try:
            # px is None, nx=True sets key only if not exists
            res = redis_client.set(key, value, ex=ttl, nx=True)
            return bool(res)
        except Exception as e:
            logger.error(f"Redis setnx error: {e}")
    
    if key in in_memory_store:
        return False
    in_memory_store[key] = value
    return True

@app.get("/health")
def health():
    return {"status": "ok", "service": "idempotency_service", "backend": "redis" if redis_client else "in_memory"}

@app.post("/idempotency/check")
def check_idempotency(req: IdempotencyCheckRequest):
    logger.info(f"Checking idempotency key: {req.idempotency_key} with hash: {req.payload_hash}")
    
    redis_key = f"idem:{req.idempotency_key}"
    
    # Structure of stored value:
    # {"payload_hash": "...", "status": "processing"|"completed", "response": {...}}
    initial_value = json.dumps({
        "payload_hash": req.payload_hash,
        "status": "processing",
        "response": None
    })
    
    # Try to set the key if it does not exist
    is_new = setnx_store(redis_key, initial_value, ttl=86400)
    
    if is_new:
        logger.info(f"New transaction lock acquired for key {req.idempotency_key}")
        return {"status": "new"}
    
    # If key already exists, retrieve and inspect
    existing_data_str = get_store(redis_key)
    if not existing_data_str:
        # Fallback if key expired between check and get
        set_store(redis_key, initial_value, ttl=86400)
        return {"status": "new"}
        
    try:
        data = json.loads(existing_data_str)
    except json.JSONDecodeError:
        logger.error(f"Failed to decode idempotency record for key {req.idempotency_key}")
        raise HTTPException(status_code=500, detail="Corrupted idempotency storage")
        
    # Check if payload matches
    if data["payload_hash"] != req.payload_hash:
        logger.warning(
            f"Idempotency key collision: {req.idempotency_key}. "
            f"Stored hash: {data['payload_hash']}, requested hash: {req.payload_hash}"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key conflict: different request payload has been submitted with this key."
        )
        
    logger.info(f"Duplicate transaction detected for key {req.idempotency_key}. Status: {data['status']}")
    return {
        "status": data["status"],
        "response": data.get("response")
    }

@app.post("/idempotency/complete")
def complete_idempotency(req: IdempotencyCompleteRequest):
    logger.info(f"Completing transaction for key {req.idempotency_key}")
    
    redis_key = f"idem:{req.idempotency_key}"
    
    # Overwrite the key with completed status and response
    value = json.dumps({
        "payload_hash": req.payload_hash,
        "status": "completed",
        "response": req.response
    })
    
    set_store(redis_key, value, ttl=86400)
    return {"status": "success"}
