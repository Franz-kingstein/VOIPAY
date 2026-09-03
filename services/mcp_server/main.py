import sys
import os
import logging
import uuid
import hashlib
import json
import httpx
import redis
from datetime import datetime, timedelta
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from typing import Optional

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
r_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=1, decode_responses=True)


# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from shared.tracing import TracingMiddleware, get_trace_id, get_tracing_headers
from shared.auth.jwt_utils import decode_access_token, create_access_token
from services.mcp_server.db import (
    init_db, get_db, DBOrder, DBMandate, DBPayment, DBAuditLog
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

class TraceLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[TraceID: {get_trace_id()}] {msg}", kwargs

logger = TraceLoggerAdapter(logging.getLogger("mcp_server"), {})

# Services URLs
RISK_ENGINE_URL = os.getenv("RISK_ENGINE_URL", "http://risk_engine:8004")
IDEMPOTENCY_SERVICE_URL = os.getenv("IDEMPOTENCY_SERVICE_URL", "http://idempotency_service:8005")
BANK_SIMULATOR_URL = os.getenv("BANK_SIMULATOR_URL", "http://bank_simulator:8003")
WEBHOOK_DISPATCHER_URL = os.getenv("WEBHOOK_DISPATCHER_URL", "http://webhook_dispatcher:8006")

# Hardcoded limit for demo safety
MAX_DEMO_AMOUNT = float(os.getenv("MAX_DEMO_AMOUNT", 5000.0))

# Initialize Database
init_db()

from mcp.server.transport_security import TransportSecuritySettings

# Create FastMCP Server
mcp_server = FastMCP(
    "razorpay-mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"]
    )
)

# Helpers for downstream HTTP calls
def call_service_post(url: str, json_data: dict) -> dict:
    headers = get_tracing_headers()
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=json_data, headers=headers)
            if resp.status_code >= 400:
                logger.error(f"Downstream service {url} returned HTTP {resp.status_code}: {resp.text}")
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text
                raise Exception(detail)
            return resp.json()
    except httpx.RequestError as e:
        logger.error(f"Downstream connection error to {url}: {e}")
        raise Exception(f"Connection failure to upstream: {url}")

def write_audit_log(trace_id: str, action: str, status: str, payload: dict):
    # Redact sensitive parameters
    clean_payload = payload.copy()
    if "token" in clean_payload:
        clean_payload["token"] = "REDACTED"
    
    db_gen = get_db()
    db = next(db_gen)
    try:
        log_entry = DBAuditLog(
            trace_id=trace_id,
            action=action,
            status=status,
            payload=json.dumps(clean_payload),
            created_at=datetime.utcnow()
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        logger.error(f"Audit log write failed: {e}")
    finally:
        db.close()

# Token validator
def validate_token(token: str) -> dict:
    payload = decode_access_token(token)
    if not payload:
        raise Exception("Authentication failure: Invalid or expired session token")
    return payload

# Tools Definition
@mcp_server.tool(name="verify_security_session", description="Inspect and verify the voice biometrics credentials for the current session. Must pass session_id.")
def verify_security_session(session_id: str) -> str:
    """Check the dynamic voice biometrics and step-up PIN verification status.
    
    Returns:
      A JSON string with 'status' (success, step_up, block) and an explanation message.
    """
    logger.info(f"verify_security_session called for session: {session_id}")
    
    try:
        # 1. Check if backup PIN has been verified
        session_key = f"session:{session_id}"
        session_str = r_client.get(session_key)
        if session_str:
            session_state = json.loads(session_str)
            if session_state.get("payment_pin_verified", False):
                logger.info(f"Security PASS: Backup PIN has been verified for session {session_id}.")
                return json.dumps({
                    "status": "success",
                    "msg": "Security verification passed via backup PIN authorization."
                })
                
        # 2. Check biometrics metadata stored in Redis
        bio_key = f"session_biometrics:{session_id}"
        bio_str = r_client.get(bio_key)
        if not bio_str:
            # Default fallback if no voice metadata recorded (e.g. text entry or not enrolled yet)
            logger.info(f"Security PASS: No voice biometric metadata found (assuming text-based or first-time transaction).")
            return json.dumps({
                "status": "success",
                "msg": "Security verification passed (no biometrics restrictions found)."
            })
            
        bio = json.loads(bio_str)
        
        # 3. Enforce Liveness spoof checks
        if bio.get("enrolled") or bio.get("is_synthetic") or bio.get("is_replay"):
            if not bio.get("liveness_passed"):
                logger.warning(f"Security BLOCK: Liveness check failed! synthetic={bio.get('is_synthetic')}, replay={bio.get('is_replay')}.")
                return json.dumps({
                    "status": "block",
                    "msg": "Access Denied. Synthetic clone or replay voice signature detected."
                })
                
        # 4. Enforce Speaker Verification with Step-up PIN fallback
        if bio.get("enrolled") and not bio.get("passed"):
            score = bio.get("biometric_score", 0.0)
            logger.warning(f"Security check failed speaker matching. Biometric score: {score:.1f}%")
            
            # Borderline Step-up range (70% - 85% match score)
            if score >= 70.0:
                logger.info(f"Biometrics score {score:.1f}% is borderline. Triggering step-up PIN authorization.")
                return json.dumps({
                    "status": "step_up",
                    "msg": "Voice biometric verification is borderline. Backup PIN authorization required."
                })
            else:
                logger.warning(f"Biometrics score {score:.1f}% failed completely.")
                return json.dumps({
                    "status": "block",
                    "msg": "Access Denied. Voice biometric profile does not match the enrolled owner."
                })
                
        logger.info(f"Security PASS: Voice biometric verification passed with score {bio.get('biometric_score', 100.0):.1f}%.")
        return json.dumps({
            "status": "success",
            "msg": "Security verification passed. Voice biometric match successful."
        })
        
    except Exception as e:
        logger.error(f"Error checking verification inside verify_security_session: {e}")
        # Default fail-safe fallback: allow but log error
        return json.dumps({
            "status": "success",
            "msg": f"Verification bypassed due to internal error: {str(e)}"
        })

@mcp_server.tool(name="create_order", description="Create an order for payment tracking. Must pass authorization token.")
def create_order(amount: float, token: str, receipt_id: str = None) -> dict:
    trace_id = get_trace_id()
    logger.info(f"create_order tool invoked: amount=₹{amount}, receipt={receipt_id}")
    
    # 1. Validate auth token
    session_data = validate_token(token)
    
    # 2. Caps for demo safety
    if amount > MAX_DEMO_AMOUNT:
        write_audit_log(trace_id, "create_order", "declined", {"amount": amount, "reason": f"Amount exceeds safety limit ₹{MAX_DEMO_AMOUNT}"})
        raise Exception(f"Transaction rejected: Amount exceeds maximum safety limit of ₹{MAX_DEMO_AMOUNT} for live demos.")
        
    # 3. Create Real Razorpay Order if key is available
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    order_id = None
    
    if key_id and key_secret:
        try:
            import httpx
            import base64
            
            logger.info("RAZORPAY: Attempting live order creation on Razorpay Test Mode API.")
            auth_str = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("utf-8")
            headers = {
                "Authorization": f"Basic {auth_str}",
                "Content-Type": "application/json"
            }
            payload = {
                "amount": int(amount * 100),  # Razorpay expects amount in paise
                "currency": "INR",
                "receipt": receipt_id or f"rec_{uuid.uuid4().hex[:10]}"
            }
            
            with httpx.Client(timeout=10.0) as client:
                resp = client.post("https://api.razorpay.com/v1/orders", json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    order_data = resp.json()
                    order_id = order_data.get("id")
                    logger.info(f"RAZORPAY: Successfully created test order on Razorpay API: {order_id}")
                else:
                    logger.error(f"RAZORPAY: Failed to create order. Status={resp.status_code}, Body={resp.text}")
        except Exception as rzp_err:
            logger.error(f"RAZORPAY: Order creation exception: {rzp_err}")
            
    # Fallback to local mock order generation if Razorpay API was not called or failed
    if not order_id:
        order_id = f"order_{uuid.uuid4().hex[:12]}"
        logger.info(f"RAZORPAY: Falling back to local mock order creation: {order_id}")
    
    db_gen = get_db()
    db = next(db_gen)
    try:
        db_order = DBOrder(
            order_id=order_id,
            amount=amount,
            currency="INR",
            receipt_id=receipt_id,
            status="created",
            created_at=datetime.utcnow()
        )
        db.add(db_order)
        db.commit()
        
        logger.info(f"Order created successfully in database: {order_id}")
        write_audit_log(trace_id, "create_order", "success", {"order_id": order_id, "amount": amount})
        return {
            "order_id": order_id,
            "amount": amount,
            "currency": "INR",
            "receipt_id": receipt_id,
            "status": "created",
            "created_at": db_order.created_at.isoformat()
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Database error creating order: {e}")
        write_audit_log(trace_id, "create_order", "failed", {"amount": amount, "error": str(e)})
        raise Exception(f"Database failure: {str(e)}")
    finally:
        db.close()

@mcp_server.tool(name="validate_mandate", description="Validate an active payment mandate with the bank simulator. Must pass authorization token.")
def validate_mandate(mandate_id: str, amount: float, token: str) -> dict:
    trace_id = get_trace_id()
    logger.info(f"validate_mandate tool invoked: mandate={mandate_id}, amount=₹{amount}")
    
    # 1. Validate auth token
    validate_token(token)
    
    # 2. Call Bank Simulator to validate mandate
    try:
        sim_response = call_service_post(
            f"{BANK_SIMULATOR_URL}/upi/validate_mandate",
            {"mandate_id": mandate_id, "amount": amount}
        )
    except Exception as e:
        write_audit_log(trace_id, "validate_mandate", "failed", {"mandate_id": mandate_id, "amount": amount, "error": str(e)})
        raise Exception(f"Bank network unreachable for mandate validation: {str(e)}")

    valid = sim_response.get("valid", False)
    reason = sim_response.get("reason", "Unknown validation response")
    
    db_gen = get_db()
    db = next(db_gen)
    try:
        if valid and "mandate" in sim_response:
            m = sim_response["mandate"]
            # Sync / Update database state of mandate
            db_mandate = db.query(DBMandate).filter(DBMandate.mandate_id == mandate_id).first()
            if not db_mandate:
                db_mandate = DBMandate(
                    mandate_id=mandate_id,
                    max_amount=m["max_amount"],
                    remaining_limit=m["remaining_limit"],
                    frequency=m["frequency"],
                    payee_name=m["payee_name"],
                    status=m["status"],
                    expires_at=datetime.fromisoformat(m["expires_at"].replace("Z", "+00:00"))
                )
                db.add(db_mandate)
            else:
                db_mandate.remaining_limit = m["remaining_limit"]
                db_mandate.status = m["status"]
            db.commit()
            
            logger.info(f"Mandate {mandate_id} validated successfully. Remaining: ₹{db_mandate.remaining_limit}")
            write_audit_log(trace_id, "validate_mandate", "success", {"mandate_id": mandate_id, "valid": True})
            return {
                "valid": True,
                "reason": reason,
                "mandate": {
                    "mandate_id": mandate_id,
                    "max_amount": db_mandate.max_amount,
                    "remaining_limit": db_mandate.remaining_limit,
                    "frequency": db_mandate.frequency,
                    "payee_name": db_mandate.payee_name,
                    "status": db_mandate.status
                }
            }
        else:
            logger.warning(f"Mandate {mandate_id} validation failed: {reason}")
            write_audit_log(trace_id, "validate_mandate", "declined", {"mandate_id": mandate_id, "valid": False, "reason": reason})
            return {"valid": False, "reason": reason}
    except Exception as e:
        db.rollback()
        logger.error(f"Database error syncing mandate: {e}")
        raise Exception(f"Database mandate sync failed: {str(e)}")
    finally:
        db.close()

@mcp_server.tool(name="execute_payment", description="Process and authorize payment against a generated order. Must pass authorization token and unique idempotency key.")
def execute_payment(
    order_id: str,
    payment_method: str,
    idempotency_key: str,
    token: str,
    mandate_id: Optional[str] = None,
    payee_name: Optional[str] = None
) -> dict:
    trace_id = get_trace_id()
    logger.info(f"execute_payment tool invoked: order={order_id}, method={payment_method}, key={idempotency_key}, mandate={mandate_id}")
    
    # 1. Validate auth token
    session_data = validate_token(token)
    session_id = session_data.get("session_id", "anonymous_session")
    
    # 2. Check LIVE_MODE guardrail
    LIVE_MODE = os.getenv("LIVE_MODE", "false").lower() == "true"
    cli_override = "--i-know-what-im-doing" in sys.argv or os.getenv("OVERRIDE_LIVE_MODE", "false").lower() == "true"
    
    if LIVE_MODE and not cli_override:
        logger.critical("LIVE_MODE=true set without override! Refusing execution.")
        raise Exception("Security Block: Real banking transaction execution is blocked. LIVE_MODE is set to true without override flag.")
        
    # 3. Create idempotency check payload hash
    payload_dict = {
        "order_id": order_id,
        "payment_method": payment_method,
        "mandate_id": mandate_id
    }
    payload_hash = hashlib.sha256(json.dumps(payload_dict, sort_keys=True).encode("utf-8")).hexdigest()
    
    # 4. Check Idempotency
    try:
        idem_response = call_service_post(
            f"{IDEMPOTENCY_SERVICE_URL}/idempotency/check",
            {"idempotency_key": idempotency_key, "payload_hash": payload_hash}
        )
    except Exception as e:
        write_audit_log(trace_id, "execute_payment", "failed", {"order_id": order_id, "error": f"Idempotency check failed: {str(e)}"})
        raise Exception(f"Idempotency engine offline: {str(e)}")
        
    status = idem_response.get("status")
    
    if status == "completed":
        logger.info(f"Idempotent hit: returning cached response for key {idempotency_key}")
        return idem_response["response"]
    elif status == "processing":
        logger.warning(f"Concurrent transaction in progress for key {idempotency_key}")
        raise Exception("Transaction Processing: A request for this transaction is already in progress. Please wait.")
        
    # 5. Fetch Order
    db_gen = get_db()
    db = next(db_gen)
    try:
        db_order = db.query(DBOrder).filter(DBOrder.order_id == order_id).first()
        if not db_order:
            raise Exception(f"Order {order_id} not found.")
            
        if db_order.status == "paid":
            raise Exception("Transaction Error: This order has already been successfully paid.")
            
        amount = db_order.amount
        
        # 5.5 Resolve final payee name
        final_payee = "Ramesh"
        if mandate_id:
            db_mandate = db.query(DBMandate).filter(DBMandate.mandate_id == mandate_id).first()
            if db_mandate:
                final_payee = db_mandate.payee_name
        elif payee_name:
            final_payee = payee_name
        else:
            if db_order.receipt_id:
                receipt_lower = db_order.receipt_id.lower()
                for name in ["balu", "ramesh", "somu"]:
                    if name in receipt_lower:
                        final_payee = name.title()
                        break
        
        # 6. Evaluate Risk Engine (Vulcan AI replacement)
        try:
            risk_response = call_service_post(
                f"{RISK_ENGINE_URL}/score",
                {
                    "amount": amount,
                    "session_id": session_id,
                    "mandate_flag": 1 if mandate_id else 0,
                    "is_new_payee": 0, # simulated payee trust factors
                    "device_trust_score": 1.0
                }
            )
        except Exception as e:
            logger.error(f"Risk evaluation offline: {e}. Defaulting to rules-based fallback.")
            # rules fallback inside MCP
            if amount > 100000.0 and not mandate_id:
                risk_response = {"score": 0.8, "decision": "decline", "reasons": ["Risk evaluation offline: High amount without mandate blocked as fallback"]}
            else:
                risk_response = {"score": 0.1, "decision": "allow", "reasons": ["Risk evaluation offline: approved via fallback"]}

        risk_score = risk_response.get("score", 0.0)
        risk_decision = risk_response.get("decision", "allow")
        risk_reasons = risk_response.get("reasons", [])
        
        if risk_decision == "decline":
            logger.warning(f"Transaction declined by Risk Engine: {risk_reasons}")
            db_order.status = "failed"
            payment_id = f"pay_{uuid.uuid4().hex[:12]}"
            db_payment = DBPayment(
                payment_id=payment_id,
                order_id=order_id,
                status="failed",
                risk_score=risk_score,
                payment_method=payment_method,
                created_at=datetime.utcnow()
            )
            db.add(db_payment)
            db.commit()
            
            # Prepare failed result
            result = {
                "payment_id": payment_id,
                "order_id": order_id,
                "status": "failed",
                "risk_score": risk_score,
                "reasons": risk_reasons,
                "utr_number": None,
                "created_at": db_payment.created_at.isoformat()
            }
            
            # Save idempotency result
            call_service_post(
                f"{IDEMPOTENCY_SERVICE_URL}/idempotency/complete",
                {"idempotency_key": idempotency_key, "payload_hash": payload_hash, "response": result}
            )
            
            # Trigger webhook dispatcher
            call_service_post(
                f"{WEBHOOK_DISPATCHER_URL}/dispatch",
                {"event_type": "payment.failed", "payload": result, "trace_id": trace_id}
            )
            
            write_audit_log(trace_id, "execute_payment", "declined", result)
            return result

        # 7. Check mandate limits if mandate_id is provided
        if mandate_id:
            try:
                mandate_check = call_service_post(
                    f"{BANK_SIMULATOR_URL}/upi/validate_mandate",
                    {"mandate_id": mandate_id, "amount": amount}
                )
                if not mandate_check.get("valid", False):
                    logger.warning(f"Payment failed: Mandate check failed: {mandate_check.get('reason')}")
                    db_order.status = "failed"
                    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
                    db_payment = DBPayment(
                        payment_id=payment_id,
                        order_id=order_id,
                        status="failed",
                        risk_score=risk_score,
                        payment_method=payment_method,
                        created_at=datetime.utcnow()
                    )
                    db.add(db_payment)
                    db.commit()
                    
                    result = {
                        "payment_id": payment_id,
                        "order_id": order_id,
                        "status": "failed",
                        "risk_score": risk_score,
                        "reasons": [f"Mandate limits exceeded: {mandate_check.get('reason')}"],
                        "utr_number": None,
                        "amount": amount,
                        "payee_name": final_payee,
                        "created_at": db_payment.created_at.isoformat()
                    }
                    
                    call_service_post(
                        f"{IDEMPOTENCY_SERVICE_URL}/idempotency/complete",
                        {"idempotency_key": idempotency_key, "payload_hash": payload_hash, "response": result}
                    )
                    
                    call_service_post(
                        f"{WEBHOOK_DISPATCHER_URL}/dispatch",
                        {"event_type": "payment.failed", "payload": result, "trace_id": trace_id}
                    )
                    
                    write_audit_log(trace_id, "execute_payment", "declined", result)
                    return result
            except Exception as e:
                logger.error(f"Mandate service connection error: {e}")
                raise Exception(f"Failed to connect to Bank Simulator mandate checks: {e}")

        # 8. Call Bank Simulator to execute payment
        payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        try:
            logger.info(f"Forwarding payment {payment_id} to NPCI switch...")
            bank_response = call_service_post(
                f"{BANK_SIMULATOR_URL}/npci/pay",
                {"amount": amount, "payee_name": final_payee, "payment_id": payment_id}
            )
        except Exception as e:
            logger.error(f"NPCI bank simulator connection error: {e}")
            raise Exception(f"NPCI core switch connection failure: {e}")

        status_outcome = bank_response.get("status", "failed")
        utr_number = bank_response.get("utr_number")
        reason = bank_response.get("reason", "Bank switch communication error")

        if status_outcome == "captured":
            db_order.status = "paid"
            db_payment = DBPayment(
                payment_id=payment_id,
                order_id=order_id,
                status="captured",
                utr_number=utr_number,
                risk_score=risk_score,
                payment_method=payment_method,
                created_at=datetime.utcnow()
            )
            event_type = "payment.captured"
        else:
            db_order.status = "failed"
            db_payment = DBPayment(
                payment_id=payment_id,
                order_id=order_id,
                status="failed",
                utr_number=None,
                risk_score=risk_score,
                payment_method=payment_method,
                created_at=datetime.utcnow()
            )
            event_type = "payment.failed"

        db.add(db_payment)
        db.commit()

        result = {
            "payment_id": payment_id,
            "order_id": order_id,
            "status": db_payment.status,
            "risk_score": risk_score,
            "reasons": [reason],
            "utr_number": utr_number,
            "amount": amount,
            "payee_name": final_payee,
            "created_at": db_payment.created_at.isoformat()
        }

        # 9. Complete Idempotency record
        try:
            call_service_post(
                f"{IDEMPOTENCY_SERVICE_URL}/idempotency/complete",
                {"idempotency_key": idempotency_key, "payload_hash": payload_hash, "response": result}
            )
        except Exception as ie:
            logger.error(f"Failed to record idempotency completion: {ie}")

        # 10. Fire webhook via Webhook Dispatcher
        try:
            call_service_post(
                f"{WEBHOOK_DISPATCHER_URL}/dispatch",
                {"event_type": event_type, "payload": result, "trace_id": trace_id}
            )
        except Exception as we:
            logger.error(f"Failed to dispatch webhook event: {we}")

        write_audit_log(trace_id, "execute_payment", db_payment.status, result)
        logger.info(f"Payment execution complete: status={db_payment.status}, UTR={utr_number}")
        return result

    except Exception as e:
        db.rollback()
        logger.error(f"Error in execute_payment tool processing: {e}")
        write_audit_log(trace_id, "execute_payment", "failed", {"order_id": order_id, "error": str(e)})
        raise e
    finally:
        db.close()

@mcp_server.tool(name="fetch_payment_status", description="Query current payment transaction and audit state. Must pass authorization token.")
def fetch_payment_status(payment_id: str, token: str) -> dict:
    trace_id = get_trace_id()
    logger.info(f"fetch_payment_status tool invoked: payment={payment_id}")
    
    # 1. Validate auth token
    validate_token(token)
    
    db_gen = get_db()
    db = next(db_gen)
    try:
        db_payment = db.query(DBPayment).filter(DBPayment.payment_id == payment_id).first()
        if not db_payment:
            raise Exception(f"Payment record {payment_id} not found.")
            
        write_audit_log(trace_id, "fetch_payment_status", "success", {"payment_id": payment_id})
        return {
            "payment_id": db_payment.payment_id,
            "order_id": db_payment.order_id,
            "status": db_payment.status,
            "utr_number": db_payment.utr_number,
            "risk_score": db_payment.risk_score,
            "payment_method": db_payment.payment_method,
            "created_at": db_payment.created_at.isoformat()
        }
    except Exception as e:
        logger.error(f"Database error fetching payment status: {e}")
        write_audit_log(trace_id, "fetch_payment_status", "failed", {"payment_id": payment_id, "error": str(e)})
        raise Exception(f"Payment status fetch failure: {str(e)}")
    finally:
        db.close()


# Build Starlette application over SSE transport
app = mcp_server.sse_app()

# Add tracing middleware and extra helper endpoints to Starlette
app.add_middleware(TracingMiddleware)

async def health(request):
    return JSONResponse({"status": "ok", "service": "mcp_server"})

async def get_token(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = body.get("session_id", f"session_{uuid.uuid4().hex[:8]}")
    # Generate short-lived token: valid for 1 hour
    token = create_access_token({"session_id": session_id})
    return JSONResponse({
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 3600
    })

# Register additional routes to standard ASGI Starlette app
app.add_route("/health", health, methods=["GET"])
app.add_route("/token", get_token, methods=["POST"])
