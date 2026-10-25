"""MCP server: JSON-RPC 2.0 over Streamable HTTP (spec 2025-11-25) at /mcp."""
import json
import uuid

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import ALLOWED_ORIGINS, SERVER_INFO, SUPPORTED_VERSIONS
from .tools import TOOLS, TOOL_INDEX, call_tool

router = APIRouter()

SESSIONS: set = set()


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_rpc(msg):
    """Returns (response_or_None, extra_headers)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _err(None, -32600, "Invalid request"), {}
    if "id" not in msg:  # notification such as notifications/initialized
        return None, {}
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    if method == "initialize":
        asked = params.get("protocolVersion")
        sid = uuid.uuid4().hex
        SESSIONS.add(sid)
        return _ok(mid, {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": "Developer operations tools: repo health, security audit, release notes.",
        }), {"Mcp-Session-Id": sid}
    if method == "ping":
        return _ok(mid, {}), {}
    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS}), {}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_INDEX:
            return _err(mid, -32602, f"Unknown tool: {name}"), {}
        try:
            data = call_tool(name, params.get("arguments") or {})
            return _ok(mid, {"content": [{"type": "text", "text": json.dumps(data)}],
                             "structuredContent": data, "isError": False}), {}
        except Exception as e:  # tool execution errors are reported in-band
            return _ok(mid, {"content": [{"type": "text", "text": f"Tool failed: {e}"}], "isError": True}), {}
    return _err(mid, -32601, f"Method not found: {method}"), {}


@router.post("/mcp")
async def mcp_post(request: Request):
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:  # DNS-rebinding protection required by the spec
        return JSONResponse(_err(None, -32600, "Origin not allowed"), status_code=403)
    try:
        msg = await request.json()
    except Exception:
        return JSONResponse(_err(None, -32700, "Parse error"), status_code=400)
    sid = request.headers.get("mcp-session-id")
    is_init = isinstance(msg, dict) and msg.get("method") == "initialize"
    if sid and sid not in SESSIONS and not is_init:
        return JSONResponse(_err(msg.get("id") if isinstance(msg, dict) else None, -32001, "Session not found"),
                            status_code=404)
    payload, headers = await run_in_threadpool(handle_rpc, msg)
    if payload is None:
        return Response(status_code=202)
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept and "application/json" not in accept:
        def sse():
            yield f"event: message\ndata: {json.dumps(payload)}\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)
    return JSONResponse(payload, headers=headers)


@router.get("/mcp")
def mcp_get():  # no server-initiated stream; spec allows 405
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
def mcp_delete(request: Request):
    SESSIONS.discard(request.headers.get("mcp-session-id", ""))
    return Response(status_code=200)
