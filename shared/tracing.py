import contextvars
import uuid
from typing import Optional
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

# Context variable to hold trace ID for the current async task
_trace_id_var = contextvars.ContextVar("trace_id", default="")

def get_trace_id() -> str:
    """Retrieve the trace ID of the current context, or generate one if empty."""
    val = _trace_id_var.get()
    if not val:
        val = str(uuid.uuid4())
        _trace_id_var.set(val)
    return val

def set_trace_id(trace_id: str) -> None:
    """Explicitly set the trace ID for the current context."""
    _trace_id_var.set(trace_id)

def get_tracing_headers() -> dict:
    """Generate HTTP headers for forwarding the trace ID to downstream services."""
    return {"X-Trace-ID": get_trace_id()}

class TracingMiddleware:
    """ASGI Middleware to extract or generate trace ID and put it in context."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        trace_id = None
        if b"x-trace-id" in headers:
            try:
                trace_id = headers[b"x-trace-id"].decode("utf-8")
            except Exception:
                pass

        if not trace_id and scope.get("query_string"):
            from urllib.parse import parse_qs
            try:
                query_params = parse_qs(scope["query_string"].decode("utf-8"))
                if "trace_id" in query_params:
                    trace_id = query_params["trace_id"][0]
            except Exception:
                pass

        if not trace_id:
            trace_id = str(uuid.uuid4())

        token = _trace_id_var.set(trace_id)
        
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                resp_headers = list(message.get("headers", []))
                has_trace_header = any(h[0].lower() == b"x-trace-id" for h in resp_headers)
                if not has_trace_header:
                    resp_headers.append((b"x-trace-id", trace_id.encode("utf-8")))
                message["headers"] = resp_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _trace_id_var.reset(token)
