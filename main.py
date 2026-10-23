import os
import json
import boto3
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="OpsPulse Alexa+ MCP Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simulated MCP Tools (Spec 2025-11-25)
MCP_TOOLS = [
    {
        "name": "fetch_repo_health",
        "description": "Scans repository for active issues and pending PRs",
        "parameters": {"repo_name": "str"}
    },
    {
        "name": "generate_release_notes",
        "description": "Generates release summary based on commit logs",
        "parameters": {"version": "str"}
    }
]

class CommandRequest(BaseModel):
    query: str

def mock_mcp_execute(tool_name: str, args: dict):
    if tool_name == "fetch_repo_health":
        return {
            "status": "healthy",
            "open_prs": 3,
            "failing_tests": 0,
            "security_alerts": "None detected"
        }
    elif tool_name == "generate_release_notes":
        return {
            "version": args.get("version", "v1.1.0"),
            "commits": [
                "fix: optimized MCP streamable HTTP handler",
                "feat: integrated AWS Bedrock intent router"
            ]
        }
    return {"status": "tool_executed"}

def call_bedrock(prompt: str, context: dict):
    """AWS Bedrock integration with graceful fallback"""
    try:
        client = boto3.client("bedrock-runtime", region_name="us-east-1")
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "messages": [
                {
                    "role": "user",
                    "content": f"You are Alexa+ Ops Copilot. The user command is: '{prompt}'. MCP Tool Output: {context}. Give a crisp, voice-friendly Alexa summary."
                }
            ]
        })
        response = client.invoke_model(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            body=body
        )
        res_body = json.loads(response["body"].read())
        return res_body["content"][0]["text"]
    except Exception:
        if "health" in prompt.lower() or "scan" in prompt.lower():
            return "Alexa+ Report: I scanned your repository via MCP. Status is clean with 3 open PRs, zero failing tests, and no security alerts."
        return "Alexa+ Report: Release notes generated for v1.1.0. 2 commits packaged including optimized MCP streamable handler and Bedrock integration."

@app.get("/mcp/tools")
def get_mcp_tools():
    return {"mcp_spec": "2025-11-25", "tools": MCP_TOOLS}

@app.post("/alexa/execute")
def process_alexa_command(req: CommandRequest):
    query = req.query.lower()
    
    if "health" in query or "scan" in query or "issue" in query:
        chosen_tool = "fetch_repo_health"
        tool_args = {"repo_name": "main-service"}
    else:
        chosen_tool = "generate_release_notes"
        tool_args = {"version": "v1.1.0"}
        
    tool_output = mock_mcp_execute(chosen_tool, tool_args)
    voice_response = call_bedrock(req.query, tool_output)
    
    return {
        "user_query": req.query,
        "mcp_event": {
            "protocol": "Streamable HTTP (2025-11-25)",
            "selected_tool": chosen_tool,
            "args": tool_args,
            "result": tool_output
        },
        "alexa_speech": voice_response
    }

os.makedirs("static", exist_ok=True)
app.mount("/", StaticFiles(directory="static", html=True), name="static")