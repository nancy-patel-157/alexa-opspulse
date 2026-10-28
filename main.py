"""OpsPulse entry point. Run: uvicorn main:app --reload --port 8000"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from opspulse.config import ALLOWED_ORIGINS, ROOT
from opspulse import bedrock_agent, mcp_server

app = FastAPI(title="OpsPulse Alexa+ MCP Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)
app.include_router(mcp_server.router)      # /mcp  (MCP, Streamable HTTP)
app.include_router(bedrock_agent.router)   # /alexa/route, /alexa/speak

# static UI: keep last so API routes win
(ROOT / "static").mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)