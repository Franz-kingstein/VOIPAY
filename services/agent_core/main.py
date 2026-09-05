import sys
import os
import logging
import json
import redis
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel
from pydantic_ai import ModelMessagesTypeAdapter

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from shared.tracing import TracingMiddleware, get_trace_id
from services.agent_core.agent import agent, AgentDeps, AgentReply, api_key
from services.agent_core.session_memory import get_session_data, save_session_data
import re
import uuid
from pydantic_ai import RunContext

async def simulate_agent_flow(message: str, deps: AgentDeps, session_id: str, is_tamil: bool = False) -> AgentReply:
    from services.agent_core.agent import create_order_tool, execute_payment_tool, validate_mandate_tool
    
    ctx = RunContext(deps=deps, model=agent.model, usage=None)
    message_lower = message.lower()
    session_state = get_session_data(session_id)
    pending = session_state.get("pending_payment")
    
    confirm_words = [
        "yes", "confirm", "ok", "sure", "yup", "correct", "pay", "proceed",
        "ஆம்", "ஆமாம்", "சரி", "அனுப்பு", "செலுத்து", "ஓகே", "aam", "aamaa", "sari"
    ]
    
    # 1. Handle confirmation
    if pending and any(word in message_lower for word in confirm_words):
        order_id = pending["order_id"]
        amount = pending["amount"]
        payee = pending.get("payee", "payee")
        
        idem_key = f"idem_{uuid.uuid4().hex[:12]}"
        
        if amount > 5000.0:
            try:
                await validate_mandate_tool(ctx, mandate_id="mandate_large_limit", amount=amount)
            except Exception as me:
                logger.error(f"Mandate validation failed: {me}")
                
        try:
            pay_res = await execute_payment_tool(
                ctx,
                order_id=order_id,
                payment_method="upi",
                idempotency_key=idem_key,
                payee_name=payee
            )
            payment_id = pay_res.get("payment_id")
            status = pay_res.get("status", "failed")
            
            if status == "captured":
                utr = pay_res.get('utr_number') or 'UTR7892398293'
                if is_tamil:
                    spoken_text = f"{payee} என்பவருக்கு {amount} ரூபாய் செலுத்துதல் வெற்றிகரமாக முடிந்தது. குறிப்பு எண்: {utr}."
                else:
                    spoken_text = f"Payment of {amount} rupees to {payee} was successful. Reference number is {utr}."
            else:
                reasons = pay_res.get("error") or "Bank declined the transaction"
                if is_tamil:
                    spoken_text = f"{payee} என்பவருக்கு {amount} ரூபாய் செலுத்துதல் தோல்வியடைந்தது. காரணம்: {reasons}."
                else:
                    spoken_text = f"Payment of {amount} rupees to {payee} failed. Reason: {reasons}."
        except Exception as pe:
            logger.error(f"Payment execution failed: {pe}")
            spoken_text = f"{payee} என்பவருக்கு {amount} ரூபாய் செலுத்துதல் தோல்வியடைந்தது." if is_tamil else f"Payment of {amount} rupees to {payee} failed due to a system error."
            payment_id = None
            
        session_state["pending_payment"] = None
        save_session_data(session_id, session_state)
        
        return AgentReply(
            spoken_text=spoken_text,
            action="done",
            order_id=order_id,
            payment_id=payment_id
        )
        
    # 2. Parse new payment request
    amount_match = re.search(r'(\d+(?:\.\d+)?)', message)
    if amount_match:
        amount = float(amount_match.group(1))
        payee = "Ramesh"
        
        m1 = re.search(r'(?:pay|send|give|transfer)\s*(?:rs\.?|rupees?|ரூபாய்|ரூ\.?)?\s*\d+(?:\.\d+)?\s*(?:rs\.?|rupees?|ரூபாய்|ரூ\.?)?\s*(?:to|for)?\s+([a-zA-Z0-9\s_\u0B80-\u0BFF]+)', message, re.IGNORECASE)
        m2 = re.search(r'([a-zA-Z0-9\s_\u0B80-\u0BFF]+)\s*(?:இற்கு|க்கு)?\s*\d+(?:\.\d+)?\s*(?:rs\.?|rupees?|ரூபாய்|ரூ\.?)?', message, re.IGNORECASE)
        
        if m1 and m1.group(1).strip():
            payee = m1.group(1).strip()
        elif m2 and m2.group(1).strip():
            payee = m2.group(1).strip()
            
        payee = re.sub(r'^(?:pay|send|give|transfer|to|for|அனுப்பு|செலுத்து|கொடு)\s+', '', payee, flags=re.IGNORECASE).strip()
        payee = re.sub(r'\s+(?:pay|send|give|transfer|to|for|அனுப்பு|செலுத்து|கொடு)$', '', payee, flags=re.IGNORECASE).strip()
        if not payee:
            payee = "Ramesh"
            
        try:
            order_res = await create_order_tool(ctx, amount=amount)
            order_id = order_res.get("order_id")
            
            session_state["pending_payment"] = {
                "amount": amount,
                "payee": payee,
                "order_id": order_id
            }
            save_session_data(session_id, session_state)
            
            if is_tamil:
                spoken_text = f"உறுதிப்படுத்தவும்: {payee} என்பவருக்கு {amount} ரூபாய் அனுப்பவா?"
            else:
                spoken_text = f"Please confirm: pay {amount} rupees to {payee}?"
                
            return AgentReply(
                spoken_text=spoken_text,
                action="confirm",
                order_id=order_id
            )
        except Exception as oe:
            logger.error(f"Order creation failed: {oe}")
            spoken_text = "மன்னித்துக்கொள்ளுங்கள், உங்களது கட்டண ஆர்டரை உருவாக்க முடியவில்லை." if is_tamil else "I'm sorry, I could not create your payment order. Please try again."
            return AgentReply(
                spoken_text=spoken_text,
                action="done",
                error=str(oe)
            )
            
    if is_tamil:
        spoken_text = "வணக்கம்! நான் உங்கள் குரல்வழி பணச்செலுத்தல் உதவிமுகவர். 'ரமேஷிற்கு 500 ரூபாய் அனுப்பு' என்று கூறலாம்."
    else:
        spoken_text = "Hello! I am your Voice-to-Pay agent. You can say: Pay 500 rupees to Ramesh."
        
    return AgentReply(
        spoken_text=spoken_text,
        action="ask"
    )

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

class TraceLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[TraceID: {get_trace_id()}] {msg}", kwargs

logger = TraceLoggerAdapter(logging.getLogger("agent_core"), {})

app = FastAPI(title="Agent Core (Pydantic AI)")
app.add_middleware(TracingMiddleware)

# MCP URL configuration
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp_server:8002/sse")

# Redis configuration for pub/sub notifications
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

redis_client = None
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=1, socket_timeout=2.0)
    redis_client.ping()
except Exception as e:
    logger.warning(f"Could not connect to Redis for pub/sub: {e}")
    redis_client = None

class ChatRequest(BaseModel):
    session_id: str
    message: str
    token: str
    metadata: Optional[dict] = None

class VerifyPinRequest(BaseModel):
    session_id: str
    pin: str

@app.get("/health")
def health():
    return {"status": "ok", "service": "agent_core"}

@app.post("/agent/verify_pin")
async def verify_pin(req: VerifyPinRequest):
    logger.info(f"PIN validation request received for session {req.session_id}")
    session_state = get_session_data(req.session_id)
    
    # Mock backup payment PIN is hardcoded to "1234" for the demo
    if req.pin == "1234":
        session_state["payment_pin_verified"] = True
        session_state["payment_confirmed"] = True  # PIN bypass automatically confirms transaction
        save_session_data(req.session_id, session_state)
        logger.info(f"PIN successfully verified for session {req.session_id}.")
        return {"status": "success", "msg": "PIN successfully verified."}
    else:
        logger.warning(f"Invalid PIN input ('{req.pin}') for session {req.session_id}.")
        raise HTTPException(status_code=400, detail="Invalid backup PIN. Access denied.")

@app.post("/agent/chat", response_model=AgentReply)
async def chat(req: ChatRequest):
    trace_id = get_trace_id()
    logger.info(f"Chat request received for session {req.session_id}: message='{req.message}'")
    
    # 1. Fetch memory state early
    session_state = get_session_data(req.session_id)
    # Store incoming biometric metadata in Redis under "session_biometrics:{session_id}"
    if req.metadata and "biometrics" in req.metadata:
        try:
            if redis_client:
                bio_key = f"session_biometrics:{req.session_id}"
                redis_client.setex(bio_key, 600, json.dumps(req.metadata["biometrics"]))
                logger.info(f"Saved incoming biometrics metadata to Redis at '{bio_key}'.")
        except Exception as re:
            logger.error(f"Failed to save biometrics metadata to Redis: {re}")
    else:
        # Clear stale biometric details if text/manual entry
        if redis_client:
            try:
                redis_client.delete(f"session_biometrics:{req.session_id}")
            except Exception:
                pass
    
    # Check if user is confirming a pending payment
    message_lower = req.message.lower()
    pending = session_state.get("pending_payment")
    
    # Proactive Reset: If starting a new payment command, clear any old stale history
    number_terms = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "rupees"]
    is_new_payment_command = any(word in message_lower for word in ["pay", "send", "give", "transfer"]) or (any(term in message_lower for term in number_terms) and any(kw in message_lower for kw in ["rupees", "pay", "to"]))
    if is_new_payment_command:
        logger.info(f"New payment command detected. Clearing old agent history to isolate transaction.")
        session_state["agent_history"] = None
        session_state["pending_payment"] = None
        session_state["payment_confirmed"] = False
        save_session_data(req.session_id, session_state)
        pending = None
        
    confirm_words = [
        "yes", "confirm", "ok", "sure", "yup", "correct", "pay", "proceed",
        "haan", "haa", "हाँ", "हा", "कर दो", "भेज दो", "सही", "ठीक",
        "ஆம்", "ஆமாம்", "சரி", "அனுப்பு", "செலுத்து", "ஓகே", "aam", "aamaa", "sari",
        "అవును", "సరే", "avunu", "ಹೌದು", "haudu", "sí", "si", "confirmar", "proceder", "pagar", "oui"
    ]
    if pending and any(word in message_lower for word in confirm_words):
        session_state["payment_confirmed"] = True
        save_session_data(req.session_id, session_state)
        logger.info(f"Payment confirmed by user voice input for session {req.session_id}: '{req.message}'")
    
    # 2. Deserialize Pydantic AI message history if present
    message_history = []
    history_json = session_state.get("agent_history")
    if history_json:
        try:
            message_history = ModelMessagesTypeAdapter.validate_json(history_json)
            logger.info(f"Loaded {len(message_history)} messages from session history.")
        except Exception as e:
            logger.warning(f"Failed to deserialize agent history: {e}. Starting fresh.")
            message_history = []
            
    # 3. Setup dependencies
    deps = AgentDeps(
        session_id=req.session_id,
        token=req.token,
        mcp_url=MCP_SERVER_URL
    )
    
    # 4. Run the Agent
    try:
        # Append cart context as system instructions if present
        cart_items = session_state.get("cart", [])
        cart_context = f"\n\nCurrent cart context: {json.dumps(cart_items)}"
        
        input_str = req.message + cart_context
        # Log the exact input string handed to the Pydantic AI Agent.run() call for every turn
        logger.info(f"Input string handed to Agent.run(): '{input_str}'")
        
        # Detect language preference
        is_hindi = any('\u0900' <= char <= '\u097F' for char in req.message)
        is_tamil = (
            any('\u0B80' <= char <= '\u0BFF' for char in req.message) or
            (req.metadata and req.metadata.get("preferred_language") == "ta") or
            any(w in req.message.lower() for w in ["anuppu", "anupavum", "roobai", "ruvai", "aam", "aamaa", "sari", "rameshuku"])
        )
        lang_code = "hi" if is_hindi else ("ta" if is_tamil else "en")
        if redis_client:
            try:
                redis_client.setex(f"session_language:{req.session_id}", 3600, lang_code)
            except Exception as re_err:
                logger.error(f"Failed to set session language in Redis: {re_err}")

        if not api_key:
            logger.info("GEMINI_API_KEY is not configured. Running offline simulation flow.")
            reply = await simulate_agent_flow(req.message, deps, req.session_id, is_tamil=is_tamil)
            session_state = get_session_data(req.session_id)
        else:
            try:
                # Append language directive hint if non-English detected
                if is_hindi:
                    input_str += "\n[System Instruction: User spoke in Hindi. You MUST generate spoken_text in native Hindi script (Devanagari).]"
                elif is_tamil:
                    input_str += "\n[System Instruction: User spoke in Tamil. You MUST generate spoken_text in native Tamil script (தமிழ்).]"
                # Run agent asynchronously
                result = await agent.run(
                    input_str,
                    deps=deps,
                    message_history=message_history
                )
                reply = result.output
                
                # Reload session state from Redis to preserve tool updates (like pending_payment)
                session_state = get_session_data(req.session_id)
                
                # Save updated message history back to Redis
                try:
                    updated_history_json = ModelMessagesTypeAdapter.dump_json(result.new_messages())
                    session_state["agent_history"] = updated_history_json.decode("utf-8")
                except Exception as se:
                    logger.error(f"Failed to serialize agent messages: {se}")
            except Exception as live_err:
                logger.error(f"CRITICAL: Live Gemini API failed! Error: {live_err}. Falling back to offline simulator.")
                reply = await simulate_agent_flow(req.message, deps, req.session_id, is_tamil=is_tamil)
                # Prepend warning prefix so it registers in the frontend console / UI
                reply.spoken_text = f"[Live API Warning: {str(live_err)}] " + reply.spoken_text
                session_state = get_session_data(req.session_id)

        # Log the full structured AgentReply output before serialization
        logger.info(f"AgentReply output: {reply}")
            
        # Log conversational turns
        session_state["turns"].append({"role": "user", "text": req.message})
        session_state["turns"].append({"role": "model", "text": reply.spoken_text})
        
        # Clean up pending state if action is complete
        if reply.action == "done":
            session_state["pending_payment"] = None
            session_state["payment_confirmed"] = False
            session_state["payment_pin_verified"] = False
            session_state["agent_history"] = None
            
        save_session_data(req.session_id, session_state)
        
        return reply
        
    except Exception as e:
        logger.error(f"Agent processing exception: {e}")
        # Return fallback reply
        return AgentReply(
            spoken_text="I'm sorry, I encountered an internal error while processing your transaction. Please try again.",
            action="done",
            error=str(e)
        )

@app.post("/webhooks/internal")
def handle_internal_webhook(payload: dict = Body(...)):
    trace_id = get_trace_id()
    logger.info(f"Internal webhook received: payload={payload}")
    
    # Retrieve order_id/payment_id to locate session
    order_id = payload.get("order_id")
    payment_id = payload.get("payment_id")
    status = payload.get("status")
    
    # We can scan or store order-to-session mappings in Redis.
    # For simulation, the payment webhook carries or publishes notification
    # let's publish to a global channel or broadcast if session_id is not explicitly mapped.
    # To map session_id, let's inspect if the payload contains it, or we can check active sessions.
    # For demo safety, we can look up order_id inside redis mapping.
    # Let's see: in execute_payment we had session_id! We can save a mapping from order_id -> session_id in Redis when creating order.
    # Let's search if the session_id is mapped in Redis:
    session_id = None
    if redis_client:
        try:
            session_id = redis_client.get(f"order_session:{order_id}")
        except Exception as re:
            logger.error(f"Failed to get order_session mapping: {re}")
            
    if not session_id:
        # Fallback default
        session_id = "default_session"
        
    logger.info(f"Resolved order {order_id} to session: {session_id}")
    
    # Store outcome in session data
    session_state = get_session_data(session_id)
    session_state["pending_notification"] = {
        "event": "payment.outcome",
        "payload": payload
    }
    save_session_data(session_id, session_state)
    
    # Publish via Redis pub/sub to notify Voice Gateway instantly
    if redis_client:
        channel_name = f"session_channel_{session_id}"
        message = json.dumps({
            "type": "payment_outcome",
            "order_id": order_id,
            "payment_id": payment_id,
            "status": status,
            "payload": payload
        })
        try:
            redis_client.publish(channel_name, message)
            logger.info(f"Published outcome notification to pub/sub channel: {channel_name}")
        except Exception as pe:
            logger.error(f"Failed to publish to Redis pub/sub channel: {pe}")
            
    return {"status": "ok"}
