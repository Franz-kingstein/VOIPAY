import os
import json
import logging
import redis
from typing import Dict, Any, Optional

logger = logging.getLogger("agent_memory")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

redis_client = None
in_memory_memory: Dict[str, str] = {}

try:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=1,  # use db 1 for session state
        socket_timeout=2.0,
        decode_responses=True
    )
    redis_client.ping()
    logger.info(f"Connected to Redis for agent session memory at {REDIS_HOST}:{REDIS_PORT}")
except Exception as e:
    logger.warning(f"Could not connect to Redis: {e}. Falling back to in-memory session storage.")
    redis_client = None

def get_session_data(session_id: str) -> Dict[str, Any]:
    key = f"session:{session_id}"
    data_str = None
    
    if redis_client:
        try:
            data_str = redis_client.get(key)
        except Exception as e:
            logger.error(f"Redis memory get failed: {e}")
            
    if not data_str:
        data_str = in_memory_memory.get(key)
        
    if data_str:
        try:
            return json.loads(data_str)
        except Exception:
            pass
            
    # Default state if not found
    default_state = {
        "cart": [],
        "turns": [],
        "pending_confirmation": None
    }
    return default_state

def save_session_data(session_id: str, data: Dict[str, Any]) -> None:
    key = f"session:{session_id}"
    data_str = json.dumps(data)
    
    if redis_client:
        try:
            redis_client.setex(key, 3600, data_str) # 1h TTL
            return
        except Exception as e:
            logger.error(f"Redis memory set failed: {e}")
            
    in_memory_memory[key] = data_str

def add_turn(session_id: str, role: str, text: str) -> None:
    state = get_session_data(session_id)
    state["turns"].append({"role": role, "text": text, "timestamp": datetime_now_str()})
    save_session_data(session_id, state)

def datetime_now_str() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat()
