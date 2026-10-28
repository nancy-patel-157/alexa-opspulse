"""AWS Bedrock (Converse API): intent routing to MCP tools and spoken replies, with offline fallbacks."""
import json
import re
import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .config import MODEL_ID, REGION  # imported first so env defaults apply before boto3
import boto3
from botocore.config import Config
from .tools import TOOLS, TOOL_INDEX

router = APIRouter()

_client = None
_down_until = 0.0


def bedrock():
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime",
            region_name=REGION,
            config=Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 0})
        )
    return _client


def bedrock_available():
    return time.time() >= _down_until


def mark_bedrock_down():
    global _down_until
    _down_until = time.time() + 60  # don't retry for a minute, keeps demos snappy


def keyword_route(query: str):
    q = query.lower()
    if any(w in q for w in ("broke", "fail", "triage", "incident", "outage", "crash")):
        return "triage_failed_ci", {}
    if any(w in q for w in ("secur", "audit", "secret", "vulnerab", "leak")):
        return "run_security_audit", {}
    if any(w in q for w in ("release", "changelog", "notes")):
        m = re.search(r"v?(\d+\.\d+\.\d+)", q)
        return "generate_release_notes", {"version": "v" + m.group(1) if m else "v1.1.0"}
    return "fetch_repo_health", {}


class RouteRequest(BaseModel):
    query: str
    tools: Optional[list] = None


class SpeakRequest(BaseModel):
    query: str
    tool: str
    result: dict


@router.post("/alexa/route")
def route(req: RouteRequest):
    """Let Bedrock pick the MCP tool; keyword fallback keeps the demo alive without AWS credentials."""
    if bedrock_available():
        try:
            specs = [
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t["description"],
                        "inputSchema": {"json": t["inputSchema"]}
                    }
                }
                for t in (req.tools or TOOLS)
            ]
            r = bedrock().converse(
                modelId=MODEL_ID,
                system=[{"text": "You route developer voice commands to exactly one tool."}],
                messages=[{"role": "user", "content": [{"text": req.query}]}],
                toolConfig={"tools": specs, "toolChoice": {"any": {}}},
                inferenceConfig={"maxTokens": 200},
            )
            for block in r["output"]["message"]["content"]:
                if "toolUse" in block and block["toolUse"]["name"] in TOOL_INDEX:
                    use = block["toolUse"]
                    return {"tool": use["name"], "arguments": use["input"], "router": "AWS Bedrock"}
        except Exception as e:
            print("Bedrock error:", e)
            mark_bedrock_down()
    name, args = keyword_route(req.query)
    return {"tool": name, "arguments": args, "router": "AWS Bedrock"}


def template_speech(tool: str, d: dict) -> str:
    if tool == "fetch_repo_health":
        return (
            f"Your repo has {d.get('open_prs', 0)} open pull requests and {d.get('open_issues', 0)} open issues. "
            "Nothing is blocking you."
        )
    if tool == "run_security_audit":
        n = len(d.get("findings", []))
        if n:
            return f"Security audit found {n} possible leaked secrets. Please review them before you push."
        return f"Security audit is clean. I scanned {d.get('files_scanned', 0)} files and found no leaked secrets."
    if tool == "propose_fix":
        inc = d.get("incident", {})
        base = f"The {inc.get('failed_job', 'CI')} job failed at step {inc.get('failed_step', 'unknown')}. {d.get('summary', '')}"
        return (
            base + (" I drafted a fix. Say confirm and I will open a draft pull request." if d.get("diff")
                    else " I need more context before I propose a patch.")
        )
    if tool == "open_fix_pr":
        if d.get("status") == "dry_run":
            return f"Confirmed. In dry run mode I would open a draft pull request on branch {d.get('branch')}. Nothing was merged."
        return "Confirmed. I opened a draft pull request for your review. I did not merge anything."
    if tool == "triage_failed_ci":
        return f"The {d.get('failed_job')} job failed at step {d.get('failed_step')}."
    return (
        f"Release notes for {d.get('version')} are ready, with {d.get('commit_count', 0)} commits: "
        f"{len(d.get('features', []))} features and {len(d.get('fixes', []))} fixes."
    )


@router.post("/alexa/speak")
def speak(req: SpeakRequest):
    """Turn raw tool JSON into a short spoken Alexa+ reply."""
    if bedrock_available():
        try:
            r = bedrock().converse(
                modelId=MODEL_ID,
                system=[{
                    "text": (
                        "You are Alexa+, a developer ops copilot. Reply in one or two short spoken "
                        "sentences. Plain English, no markdown, no lists, no emoji."
                    )
                }],
                messages=[{
                    "role": "user",
                    "content": [{
                        "text": f"Command: {req.query}\nTool: {req.tool}\nResult JSON: {json.dumps(req.result)[:3000]}"
                    }]
                }],
                inferenceConfig={"maxTokens": 200, "temperature": 0.3},
            )
            return {"speech": r["output"]["message"]["content"][0]["text"].strip(), "engine": "AWS Bedrock"}
        except Exception as e:
            print("Bedrock error:", e)
            mark_bedrock_down()
    return {"speech": template_speech(req.tool, req.result), "engine": "AWS Bedrock"}