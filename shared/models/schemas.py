from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

class Order(BaseModel):
    order_id: str
    amount: float = Field(..., description="Amount in INR (Rupees)")
    currency: str = "INR"
    receipt_id: Optional[str] = None
    status: str = "created"  # created, paid, failed
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Mandate(BaseModel):
    mandate_id: str
    max_amount: float
    remaining_limit: float
    frequency: str  # single_use, daily, weekly, monthly
    payee_name: str
    status: str = "active"  # active, exhausted, expired
    expires_at: datetime

class PaymentResult(BaseModel):
    payment_id: str
    order_id: str
    status: str  # captured, failed
    utr_number: Optional[str] = None
    risk_score: float = 0.0
    payment_method: str = "upi"
    created_at: datetime = Field(default_factory=datetime.utcnow)

class WebhookEvent(BaseModel):
    event_id: str
    event_type: str  # payment.captured, payment.failed, order.created
    payload: dict
    signature: str
    trace_id: str
    timestamp: float = Field(default_factory=lambda: datetime.utcnow().timestamp())

class RiskScore(BaseModel):
    score: float = Field(..., description="Fraud probability score between 0.0 and 1.0")
    reasons: List[str] = Field(default_factory=list)
    decision: str = Field(..., description="allow, review, or decline")
