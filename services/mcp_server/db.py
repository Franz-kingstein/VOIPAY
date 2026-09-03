import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Float, DateTime, Integer, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/razorpay_mock")

# Robust fallback to SQLite if PostgreSQL is not available
if not DATABASE_URL.startswith("postgresql"):
    DATABASE_URL = "sqlite:///mock_razorpay.db"

engine = None
SessionLocal = None
Base = declarative_base()

# Define Database Models
class DBOrder(Base):
    __tablename__ = "orders"
    order_id = Column(String, primary_key=True, index=True)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="INR")
    receipt_id = Column(String, nullable=True)
    status = Column(String, default="created")  # created, paid, failed
    created_at = Column(DateTime, default=datetime.utcnow)

class DBMandate(Base):
    __tablename__ = "mandates"
    mandate_id = Column(String, primary_key=True, index=True)
    max_amount = Column(Float, nullable=False)
    remaining_limit = Column(Float, nullable=False)
    frequency = Column(String, nullable=False)  # daily, weekly, monthly, single_use
    payee_name = Column(String, nullable=False)
    status = Column(String, default="active")  # active, exhausted, expired
    expires_at = Column(DateTime, nullable=False)

class DBPayment(Base):
    __tablename__ = "payments"
    payment_id = Column(String, primary_key=True, index=True)
    order_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False)  # captured, failed
    utr_number = Column(String, nullable=True)
    risk_score = Column(Float, default=0.0)
    payment_method = Column(String, default="upi")
    created_at = Column(DateTime, default=datetime.utcnow)

class DBAuditLog(Base):
    __tablename__ = "audit_logs"
    log_id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String, index=True)
    action = Column(String, nullable=False)  # e.g., create_order, execute_payment
    status = Column(String, nullable=False)  # success, failed
    payload = Column(Text, nullable=True)  # JSON serialized details
    created_at = Column(DateTime, default=datetime.utcnow)

def init_db():
    global engine, SessionLocal
    # In SQLite, enforce same thread check disabled for multithreaded fastmcp calls
    connect_args = {}
    if DATABASE_URL.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        
    try:
        engine = create_engine(DATABASE_URL, connect_args=connect_args)
        # Verify connection
        engine.connect()
    except Exception as e:
        print(f"Failed to connect to database {DATABASE_URL}: {e}. Falling back to SQLite.")
        fallback_url = "sqlite:///mock_razorpay.db"
        engine = create_engine(fallback_url, connect_args={"check_same_thread": False})
        
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

def get_db():
    if not SessionLocal:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
