import os
import sys
import yaml
import hmac
import hashlib
import json
import logging
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Query, Depends
from sqlmodel import SQLModel, Session, create_engine, select, text
import redis

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from shared.models.schemas import WebhookEvent
from shared.tracing import TracingMiddleware, get_trace_id
from services.ledger_service.models import Category, ExpenseEntry, CategoryNature
from services.ledger_service.categorizer import categorize
from services.ledger_service.aggregations import (
    get_total_spend, get_spend_by_category, get_spend_by_nature,
    get_top_category, parse_period_range
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

class TraceLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[TraceID: {get_trace_id()}] {msg}", kwargs

logger = TraceLoggerAdapter(logging.getLogger("ledger_service"), {})

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/razorpay_mock")
engine = create_engine(DATABASE_URL, echo=False)

# Redis client configuration
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
redis_client = None
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=1, socket_timeout=2.0, decode_responses=True)
    redis_client.ping()
    logger.info(f"Connected to Redis for session maps at {REDIS_HOST}:{REDIS_PORT}")
except Exception as e:
    logger.warning(f"Could not connect to Redis from ledger_service: {e}")
    redis_client = None

# Webhook verification secret
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "razorpay_webhook_secret_key_2026")

def seed_categories():
    yaml_path = os.path.join(os.path.dirname(__file__), "static", "default_categories.yaml")
    if not os.path.exists(yaml_path):
        logger.warning(f"Seed categories config file not found at {yaml_path}")
        return
        
    with open(yaml_path, "r") as f:
        try:
            cats_data = yaml.safe_load(f) or []
        except Exception as ye:
            logger.error(f"Failed to parse default_categories.yaml: {ye}")
            return
            
    with Session(engine) as session:
        for item in cats_data:
            name = item.get("name")
            nature_str = item.get("nature", "need").lower()
            try:
                nature = CategoryNature(nature_str)
            except ValueError:
                nature = CategoryNature.NEED
                
            # Insert non-destructively (check if category exists by name)
            existing = session.exec(select(Category).where(Category.name == name)).first()
            if not existing:
                category = Category(name=name, nature=nature)
                session.add(category)
                logger.info(f"Seeding database category: {name} ({nature.value})")
        session.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create schema and metadata tables in PostgreSQL
    with Session(engine) as session:
        try:
            session.exec(select(1)).first()  # verify connectivity
            session.execute(text("CREATE SCHEMA IF NOT EXISTS ledger;"))
            session.commit()
            logger.info("PostgreSQL schema 'ledger' verified.")
        except Exception as e:
            logger.error(f"Failed to initialize PostgreSQL schema 'ledger': {e}")
            
    # 2. Build tables
    try:
        SQLModel.metadata.create_all(engine)
        logger.info("Ledger service tables created successfully.")
    except Exception as e:
        logger.error(f"Failed to create SQLModel metadata tables: {e}")
        
    # 3. Seed default taxonomy
    try:
        seed_categories()
    except Exception as e:
        logger.error(f"Failed to seed categories: {e}")
        
    yield

app = FastAPI(title="Expense Ledger Service", lifespan=lifespan)
app.add_middleware(TracingMiddleware)

def get_db():
    with Session(engine) as session:
        yield session

def verify_signature(body_bytes: bytes, signature: str) -> bool:
    try:
        computed = hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            body_bytes,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, signature)
    except Exception as e:
        logger.error(f"Signature calculation failure: {e}")
        return False

def extract_label_from_session(session_state: dict) -> Optional[str]:
    if not session_state or "turns" not in session_state:
        return None
    confirm_words = {"yes", "confirm", "ok", "sure", "yup", "correct", "pay", "proceed", "हां", "हाँ", "sí", "si", "ஆமாம்"}
    # Search backwards for the user's primary command
    for turn in reversed(session_state["turns"]):
        if turn.get("role") == "user":
            text_val = turn.get("text", "").strip()
            if text_val.lower() not in confirm_words and len(text_val.split()) > 1:
                return text_val
    return None

@app.get("/health")
def health():
    return {"status": "ok", "service": "ledger_service"}

@app.post("/webhooks/ledger")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    signature = request.headers.get("X-Razorpay-Signature")
    if not signature:
        logger.warning("Rejecting ledger webhook: Missing signature header")
        raise HTTPException(status_code=400, detail="Missing signature header")
        
    body_bytes = await request.body()
    if not verify_signature(body_bytes, signature):
        logger.warning("Rejecting ledger webhook: Signature verification mismatch")
        raise HTTPException(status_code=401, detail="Signature mismatch")
        
    try:
        event_dict = json.loads(body_bytes.decode("utf-8"))
        event_dict["signature"] = signature
        event = WebhookEvent(**event_dict)
    except Exception as e:
        logger.error(f"Failed to load WebhookEvent schema: {e}")
        raise HTTPException(status_code=400, detail="Invalid WebhookEvent body")
        
    # We only care about successful payment captures
    if event.event_type != "payment.captured":
        logger.info(f"Ignoring irrelevant webhook event type: {event.event_type}")
        return {"status": "ignored", "event_id": event.event_id}
        
    payload = event.payload
    payment_id = payload.get("payment_id")
    order_id = payload.get("order_id")
    amount = float(payload.get("amount", 0.0))
    payee = payload.get("payee_name")
    occurred_at_str = payload.get("created_at")
    
    try:
        occurred_at = datetime.fromisoformat(occurred_at_str.replace("Z", "+00:00")) if occurred_at_str else datetime.utcnow()
    except Exception:
        occurred_at = datetime.utcnow()
        
    if not payment_id or not order_id:
        logger.error("Missing critical identifiers (payment_id/order_id) in webhook payload")
        raise HTTPException(status_code=400, detail="Missing key fields in webhook payload")
        
    # Deduplication checks: idempotent inserts
    existing = db.exec(select(ExpenseEntry).where(ExpenseEntry.payment_id == payment_id)).first()
    if existing:
        logger.info(f"Idempotency Guard: Payment {payment_id} already ingested. Skipping.")
        return {"status": "duplicate", "payment_id": payment_id}
        
    # Resolve session_id from Redis order session map
    session_id = None
    if redis_client:
        try:
            session_id = redis_client.get(f"order_session:{order_id}")
            if isinstance(session_id, bytes):
                session_id = session_id.decode("utf-8")
        except Exception as re:
            logger.error(f"Failed to query Redis order session map: {re}")
            
    if not session_id:
        logger.warning(f"Unable to resolve session ID for order {order_id} via Redis. Using fallback ID.")
        session_id = f"fallback_{order_id}"
        
    # Resolve original spoken request label
    label = None
    if redis_client:
        try:
            session_str = redis_client.get(f"session:{session_id}")
            if session_str:
                if isinstance(session_str, bytes):
                    session_str = session_str.decode("utf-8")
                session_state = json.loads(session_str)
                label = extract_label_from_session(session_state)
        except Exception as se:
            logger.error(f"Failed to fetch session state for label extraction: {se}")
            
    # Determine category taxonomy classification
    categories = db.exec(select(Category)).all()
    category_id, nature = categorize(payee, label, categories)
    
    # Store expense record
    entry = ExpenseEntry(
        payment_id=payment_id,
        session_id=session_id,
        amount=amount,
        payee=payee,
        category_id=category_id,
        nature=nature,
        label=label,
        trace_id=event.trace_id,
        occurred_at=occurred_at
    )
    
    db.add(entry)
    db.commit()
    logger.info(f"Ingested payment {payment_id} as category ID {category_id} ({nature.value})")
    
    return {"status": "success", "payment_id": payment_id, "category_id": category_id}

@app.get("/expenses/summary")
def get_expenses_summary(
    session_id: str = Query(..., description="Current session ID"),
    period: str = Query("week", regex="^(week|month)$"),
    db: Session = Depends(get_db)
):
    start, end = parse_period_range(period)
    total = get_total_spend(db, session_id, start, end)
    by_category = get_spend_by_category(db, session_id, start, end)
    by_nature = get_spend_by_nature(db, session_id, start, end)
    
    return {
        "period": period,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "total_spending": total,
        "by_category": [c.dict() for c in by_category],
        "by_nature": {k.value: v for k, v in by_nature.items()}
    }

@app.get("/expenses/by-category")
def get_expenses_by_category(
    session_id: str = Query(..., description="Current session ID"),
    period: str = Query("week", regex="^(week|month)$"),
    db: Session = Depends(get_db)
):
    start, end = parse_period_range(period)
    by_category = get_spend_by_category(db, session_id, start, end)
    top_category = get_top_category(db, session_id, start, end)
    
    return {
        "period": period,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "by_category": [c.dict() for c in by_category],
        "top_category": top_category.dict() if top_category else None
    }

@app.get("/expenses")
def get_expenses_raw(
    session_id: str = Query(..., description="Current session ID"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    stmt = (
        select(ExpenseEntry)
        .where(ExpenseEntry.session_id == session_id)
        .order_by(ExpenseEntry.occurred_at.desc())
        .limit(limit)
    )
    entries = db.exec(stmt).all()
    results = []
    for entry in entries:
        cat_name = "Other"
        if entry.category_id:
            cat = db.get(Category, entry.category_id)
            if cat:
                cat_name = cat.name
        results.append({
            "id": entry.id,
            "payment_id": entry.payment_id,
            "amount": entry.amount,
            "payee": entry.payee,
            "category": cat_name,
            "nature": entry.nature.value,
            "label": entry.label,
            "occurred_at": entry.occurred_at.isoformat(),
            "created_at": entry.created_at.isoformat()
        })
    return results
