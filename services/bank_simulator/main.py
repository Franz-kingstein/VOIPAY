import sys
import os
import logging
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel

# Add workspace root to sys.path to allow importing from shared/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from shared.tracing import TracingMiddleware, get_trace_id
from services.bank_simulator.npci_switch import NPCISwitch
from services.bank_simulator.upi_reserve_pay import UPIReservePay

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

# Custom logging adapter to inject trace_id automatically
class TraceLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[TraceID: {get_trace_id()}] {msg}", kwargs

logger = TraceLoggerAdapter(logging.getLogger("bank_simulator"), {})

app = FastAPI(title="Bank Simulator (NPCI/UPI/ReservePay)")
app.add_middleware(TracingMiddleware)

# Initialize simulator components
npci = NPCISwitch("config.yaml")
upi = UPIReservePay("config.yaml")

class NPCIPaymentRequest(BaseModel):
    amount: float
    payee_name: str
    payment_id: str

class MandateCreateRequest(BaseModel):
    mandate_id: str
    max_amount: float
    payee_name: str
    frequency: str = "monthly"

class MandateValidateRequest(BaseModel):
    mandate_id: str
    amount: float

@app.get("/health")
def health():
    return {"status": "ok", "service": "bank_simulator"}

@app.post("/npci/pay")
async def npci_pay(req: NPCIPaymentRequest):
    logger.info(f"Processing NPCI debit request for payment {req.payment_id}: amount=₹{req.amount}, payee={req.payee_name}")
    try:
        result = await npci.process_debit(req.amount, req.payee_name)
        logger.info(f"NPCI debit outcome: {result['status']}, UTR={result.get('utr_number')}")
        return result
    except Exception as e:
        logger.error(f"Error processing NPCI pay: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/upi/mandate")
def create_mandate(req: MandateCreateRequest):
    logger.info(f"Creating UPI delegated mandate {req.mandate_id}: max_amount=₹{req.max_amount}, payee={req.payee_name}")
    try:
        mandate = upi.create_mandate(
            mandate_id=req.mandate_id,
            max_amount=req.max_amount,
            payee_name=req.payee_name,
            frequency=req.frequency
        )
        return mandate
    except ValueError as ve:
        logger.warning(f"Mandate creation validation failed: {str(ve)}")
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Error creating mandate: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/upi/mandate/{mandate_id}")
def get_mandate(mandate_id: str):
    mandate = upi.get_mandate(mandate_id)
    if not mandate:
        raise HTTPException(status_code=404, detail="Mandate not found")
    return mandate

@app.post("/upi/validate_mandate")
def validate_mandate(req: MandateValidateRequest):
    logger.info(f"Validating UPI delegated mandate {req.mandate_id} for amount ₹{req.amount}")
    result = upi.validate_and_debit(req.mandate_id, req.amount)
    if not result["valid"]:
        logger.warning(f"Mandate validation failed: {result['reason']}")
    else:
        logger.info(f"Mandate validation successful. Remaining limit: ₹{result['mandate']['remaining_limit']}")
    return result
