import sys
import os
import logging
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from shared.tracing import TracingMiddleware, get_trace_id
from shared.models.schemas import RiskScore
from services.risk_engine.rules import evaluate_hard_rules
from services.risk_engine.model import RiskModelWrapper
from services.risk_engine.train_synthetic import train_and_save_model

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

class TraceLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[TraceID: {get_trace_id()}] {msg}", kwargs

logger = TraceLoggerAdapter(logging.getLogger("risk_engine"), {})

app = FastAPI(title="Risk Forge Microservice")
app.add_middleware(TracingMiddleware)

# Initialize model wrapper (will auto-train if missing)
model_wrapper = RiskModelWrapper("model.pkl")

class TransactionFeatures(BaseModel):
    amount: float
    session_id: str
    hour_of_day: Optional[int] = Field(default_factory=lambda: datetime.utcnow().hour)
    velocity_last_1h: int = 1
    is_new_payee: int = 0
    device_trust_score: float = 1.0
    mandate_flag: int = 0

@app.get("/health")
def health():
    return {"status": "ok", "service": "risk_engine"}

@app.post("/score", response_model=RiskScore)
def evaluate_transaction_risk(txn: TransactionFeatures):
    logger.info(
        f"Evaluating risk for session {txn.session_id}: amount=₹{txn.amount}, "
        f"new_payee={txn.is_new_payee}, trust_score={txn.device_trust_score}, mandate={txn.mandate_flag}"
    )
    
    # 1. Evaluate hard coded heuristics rules
    rule_decision, rule_reasons = evaluate_hard_rules(
        amount=txn.amount,
        velocity_last_1h=txn.velocity_last_1h,
        mandate_flag=txn.mandate_flag,
        session_id=txn.session_id
    )
    
    if rule_decision == "decline":
        logger.warning(f"Heuristic rules declined transaction. Reasons: {rule_reasons}")
        return RiskScore(score=1.0, reasons=rule_reasons, decision="decline")

    # 2. Run machine learning model prediction
    try:
        anomaly_score = model_wrapper.predict_risk(
            amount=txn.amount,
            hour_of_day=txn.hour_of_day,
            velocity_last_1h=txn.velocity_last_1h,
            is_new_payee=txn.is_new_payee,
            device_trust_score=txn.device_trust_score,
            mandate_flag=txn.mandate_flag
        )
    except Exception as e:
        logger.error(f"Error executing IsolationForest prediction: {str(e)}")
        # Fallback to rules only
        anomaly_score = 0.0
        
    logger.info(f"Model anomaly score calculated: {anomaly_score:.4f}")
    
    decision = rule_decision
    reasons = list(rule_reasons)
    
    # ML thresholds
    if anomaly_score > 0.75:
        decision = "decline"
        reasons.append(f"MODEL_ANOMALY: High anomaly probability score ({anomaly_score:.2f}) from RiskForge model.")
    elif anomaly_score > 0.50 and decision != "decline":
        decision = "review"
        reasons.append(f"MODEL_ANOMALY: Moderate anomaly probability score ({anomaly_score:.2f}). Requires review.")
        
    if not reasons and decision == "allow":
        reasons.append("RISK_OK: Transaction metrics within normal limits")
        
    logger.info(f"Final risk decision: {decision}. Reasons: {reasons}")
    return RiskScore(score=anomaly_score, reasons=reasons, decision=decision)

@app.post("/train")
def train_model():
    logger.info("Manual training request received. Re-training risk model...")
    try:
        train_and_save_model("model.pkl")
        global model_wrapper
        model_wrapper = RiskModelWrapper("model.pkl")
        return {"status": "success", "message": "Model retrained and reloaded successfully."}
    except Exception as e:
        logger.error(f"Failed to retrain model: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrain model: {str(e)}")
