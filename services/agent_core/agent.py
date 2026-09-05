import os
import json
import logging
from dataclasses import dataclass
from typing import Literal, Optional, List
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.test import TestModel


from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger("agent_core")

# Pydantic AI Agent Output Type
class AgentReply(BaseModel):
    spoken_text: str = Field(..., description="Speech response text to be converted to voice for the user")
    action: Literal["ask", "confirm", "execute", "done", "step_up"] = Field(
        ...,
        description="Current conversational state: ask (clarifying info), confirm (waiting for user voice yes/no confirmation), execute (initiating tool execution), done (payment complete/failed), step_up (borderline biometric, waiting for backup PIN)"
    )
    order_id: Optional[str] = Field(default=None, description="The order identifier if created")
    payment_id: Optional[str] = Field(default=None, description="The payment identifier if executed")
    error: Optional[str] = Field(default=None, description="Error details if failure occurred")

@dataclass
class AgentDeps:
    session_id: str
    token: str
    mcp_url: str

# Model configuration: check GOOGLE_API_KEY or GEMINI_API_KEY
api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
if api_key:
    os.environ["GOOGLE_API_KEY"] = api_key
    model = GoogleModel("gemini-flash-lite-latest")
    logger.info("Initializing agent with GoogleModel (gemini-flash-lite-latest)")
else:
    model = TestModel()
    logger.warning("GOOGLE_API_KEY / GEMINI_API_KEY not found. Agent initializing with Mock TestModel.")

# Create the Agent
agent = Agent(
    model,
    deps_type=AgentDeps,
    output_type=AgentReply,
    system_prompt=(
        "You are an intelligent voice-driven payment assistant. Your goals:\n"
        "1. Security Verification Gate: Before confirming details or executing any payment, you MUST ALWAYS call the 'verify_security_session' tool to inspect the biometrics validation status.\n"
        "   - If the tool returns status='block', you MUST immediately set action='done' and set spoken_text to: 'Security validation failed: Voice biometric profile does not match the enrolled owner. Access denied.' (or the equivalent in the user's language).\n"
        "   - If the tool returns status='step_up', you MUST immediately set action='step_up' and set spoken_text to: 'Voice verification incomplete. Please enter your backup 4-digit PIN on screen.' (or the equivalent in the user's language).\n"
        "   - If the tool returns status='success', only then may you proceed with the transaction request.\n"
        "2. Always confirm the payee name and exact amount in rupees before executing any payments.\n"
        "3. Never skip calling 'validate_mandate' if the payment is recurring or large (> ₹5,000).\n"
        "4. If the user request is ambiguous (e.g. name matches multiple contacts), ask clarifying questions in the user's language (action='ask').\n"
        "5. First step to process a request is to call 'create_order' to obtain an order_id.\n"
        "6. Next, present the details and explicitly ask for confirmation in the user's language (action='confirm', e.g. 'Confirm: pay 500 rupees to Ramesh?').\n"
        "7. If the user confirms (e.g. 'yes', 'हाँ', 'ஆம்', 'sí'), generate a unique idempotency key and execute payment via 'execute_payment'. Set action='execute'.\n"
        "9. MANDATORY MULTILINGUAL DIRECTIVE:\n"
        "   - VOIPAY is fully multilingual and supports any language spoken by the user (including English, Hindi, Tamil, Telugu, Kannada, Spanish, French, etc.).\n"
        "   - You MUST detect the user's input language and respond in the EXACT same language and native script used by the user.\n"
        "   - For example, if the user speaks Hindi (Devanagari script), reply in Hindi (Devanagari script). If Tamil, reply in Tamil. If Spanish, reply in Spanish. If English, reply in English.\n"
        "You can also answer questions about the user's past spending using get_spending_summary and get_top_spending_category. Only call these when the user asks about spending history, budgets, or categories — never as part of the payment execution flow.\n"
        "Keep responses friendly, clear, and concise in the user's spoken language."
    )
)

# Helper function to call the remote MCP server tools
async def call_mcp_tool(mcp_url: str, tool_name: str, arguments: dict) -> dict:
    logger.info(f"Connecting to MCP SSE server at {mcp_url} to call: {tool_name}")
    try:
        # Establish client connection
        async with sse_client(mcp_url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                logger.info(f"Calling tool: {tool_name} with arguments: {arguments}")
                result = await session.call_tool(tool_name, arguments)
                
                if not result.content:
                    logger.warning(f"MCP tool {tool_name} returned empty content.")
                    return {}
                
                content_block = result.content[0]
                # FastMCP returns text block with serialized JSON
                if hasattr(content_block, "text"):
                    try:
                        return json.loads(content_block.text)
                    except Exception:
                        return {"text": content_block.text}
                return {"raw_response": str(content_block)}
    except Exception as e:
        logger.error(f"Error calling MCP server tool {tool_name}: {e}")
        raise Exception(f"MCP server communication failed: {str(e)}")

# Bind MCP tools to Agent
@agent.tool(name="verify_security_session")
async def verify_security_session_tool(ctx: RunContext[AgentDeps]) -> dict:
    """Inspect and verify the voice biometrics credentials for the current session to ensure the speaker matches the enrolled owner."""
    return await call_mcp_tool(
        mcp_url=ctx.deps.mcp_url,
        tool_name="verify_security_session",
        arguments={
            "session_id": ctx.deps.session_id
        }
    )

@agent.tool(name="create_order")
async def create_order_tool(ctx: RunContext[AgentDeps], amount: float, receipt_id: str = None) -> dict:
    """Create a new transaction order on Razorpay MCP."""
    res = await call_mcp_tool(
        mcp_url=ctx.deps.mcp_url,
        tool_name="create_order",
        arguments={
            "amount": amount,
            "token": ctx.deps.token,
            "receipt_id": receipt_id
        }
    )
    order_id = res.get("order_id")
    if order_id:
        from services.agent_core.session_memory import get_session_data, save_session_data, redis_client
        if redis_client:
            try:
                redis_client.setex(f"order_session:{order_id}", 3600, ctx.deps.session_id)
                logger.info(f"Mapped order_session:{order_id} to session {ctx.deps.session_id} in Redis")
            except Exception as re:
                logger.error(f"Failed to map order session in Redis: {re}")
                
        # Save pending payment state
        try:
            session_state = get_session_data(ctx.deps.session_id)
            session_state["pending_payment"] = {
                "order_id": order_id,
                "amount": amount
            }
            session_state["payment_confirmed"] = False
            save_session_data(ctx.deps.session_id, session_state)
            logger.info(f"Saved pending payment status for session {ctx.deps.session_id}")
        except Exception as se:
            logger.error(f"Failed to save pending payment details: {se}")
            
    return res

@agent.tool(name="validate_mandate")
async def validate_mandate_tool(ctx: RunContext[AgentDeps], mandate_id: str, amount: float) -> dict:
    """Validate payment mandate and check limits."""
    return await call_mcp_tool(
        mcp_url=ctx.deps.mcp_url,
        tool_name="validate_mandate",
        arguments={
            "mandate_id": mandate_id,
            "amount": amount,
            "token": ctx.deps.token
        }
    )

@agent.tool(name="execute_payment")
async def execute_payment_tool(
    ctx: RunContext[AgentDeps],
    order_id: str,
    payment_method: str,
    idempotency_key: str,
    mandate_id: str = None,
    payee_name: str = None
) -> dict:
    """Execute and finalize payment transaction against an order."""
    from services.agent_core.session_memory import get_session_data
    session_state = get_session_data(ctx.deps.session_id)
    if not session_state.get("payment_confirmed"):
        logger.warning(f"Payment execution blocked: session {ctx.deps.session_id} has not explicitly confirmed the transaction yet.")
        return {
            "status": "failed",
            "error": "Payment requires explicit user voice confirmation. Stop calling tools and ask the user 'Confirm: pay [amount] to [payee]?' first."
        }
        
    return await call_mcp_tool(
        mcp_url=ctx.deps.mcp_url,
        tool_name="execute_payment",
        arguments={
            "order_id": order_id,
            "payment_method": payment_method,
            "idempotency_key": idempotency_key,
            "token": ctx.deps.token,
            "mandate_id": mandate_id,
            "payee_name": payee_name
        }
    )

@agent.tool(name="fetch_payment_status")
async def fetch_payment_status_tool(ctx: RunContext[AgentDeps], payment_id: str) -> dict:
    """Fetch status for an existing payment transaction."""
    return await call_mcp_tool(
        mcp_url=ctx.deps.mcp_url,
        tool_name="fetch_payment_status",
        arguments={
            "payment_id": payment_id,
            "token": ctx.deps.token
        }
    )

# --- EXPENSE LEDGER SERVICE INTEGRATION ---
LEDGER_SERVICE_URL = os.getenv("LEDGER_SERVICE_URL", "http://ledger_service:8089")

class CategoryTotal(BaseModel):
    category_id: Optional[int] = None
    category_name: str
    amount: float

class SpendingSummary(BaseModel):
    period: str
    total_spending: float
    by_category: List[CategoryTotal]
    by_nature: dict

@agent.tool(name="get_spending_summary")
async def get_spending_summary(ctx: RunContext[AgentDeps], period: Literal["week", "month"]) -> SpendingSummary:
    """Fetch total spend and category breakdown for the user's current session
    over the given period, by calling ledger_service. Use 'week' or 'month'."""
    url = f"{LEDGER_SERVICE_URL}/expenses/summary?session_id={ctx.deps.session_id}&period={period}"
    logger.info(f"get_spending_summary tool calling ledger_service: {url}")
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code != 200:
                logger.error(f"ledger_service returned HTTP {resp.status_code}: {resp.text}")
                return SpendingSummary(period=period, total_spending=0.0, by_category=[], by_nature={})
            data = resp.json()
            return SpendingSummary(
                period=data.get("period", period),
                total_spending=data.get("total_spending", 0.0),
                by_category=[CategoryTotal(**c) for c in data.get("by_category", [])],
                by_nature=data.get("by_nature", {})
            )
    except Exception as e:
        logger.error(f"Error calling ledger_service for spending summary: {e}")
        return SpendingSummary(period=period, total_spending=0.0, by_category=[], by_nature={})

@agent.tool(name="get_top_spending_category")
async def get_top_spending_category(ctx: RunContext[AgentDeps], period: Literal["week", "month"]) -> Optional[CategoryTotal]:
    """Fetch the single largest expense category for the given period. Use 'week' or 'month'."""
    url = f"{LEDGER_SERVICE_URL}/expenses/by-category?session_id={ctx.deps.session_id}&period={period}"
    logger.info(f"get_top_spending_category tool calling ledger_service: {url}")
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code != 200:
                logger.error(f"ledger_service returned HTTP {resp.status_code}: {resp.text}")
                return None
            data = resp.json()
            top = data.get("top_category")
            if top:
                return CategoryTotal(**top)
            return None
    except Exception as e:
        logger.error(f"Error calling ledger_service for top category: {e}")
        return None
