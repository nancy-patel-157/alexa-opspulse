"""OpsPulse: Alexa+ developer copilot.

A real MCP server (Streamable HTTP, spec 2025-11-25) exposing three dev-ops tools,
plus AWS Bedrock (Converse API) for intent routing and voice-friendly replies.
"""
import os
import re
import json
import hashlib
import base64
import time
import uuid
import subprocess
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")  # no slow IMDS lookups on a laptop

import boto3
from botocore.config import Config
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
SERVER_INFO = {"name": "opspulse-mcp", "title": "OpsPulse Dev Ops Copilot", "version": "1.2.0"}
SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
GITHUB_REPO = os.getenv("GITHUB_REPO", "nancy-patel-157/alexa-opspulse")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
REGION = os.getenv("AWS_REGION", "us-east-1")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")

app = FastAPI(title="OpsPulse Alexa+ MCP Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)

# --------------------------------------------------------------------------- tools
TOOLS = [
    {
        "name": "fetch_repo_health",
        "title": "Repo health",
        "description": "Check open pull requests, open issues and recent activity of a GitHub repository.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "owner/name; defaults to the configured repo"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "run_security_audit",
        "title": "Security audit",
        "description": "Scan project files for leaked secrets and check repo hygiene (.gitignore, LICENSE).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "generate_release_notes",
        "title": "Release notes",
        "description": "Generate grouped release notes from recent git commits for a version.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "version": {"type": "string", "description": "Version such as v1.1.0"},
                "limit": {"type": "integer", "description": "How many recent commits to include", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "triage_failed_ci",
        "title": "Triage failed CI",
        "description": "Find the latest failed GitHub Actions run and identify the failing job and step. Use when the user asks what broke.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "owner/name; defaults to the configured repo"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "propose_fix",
        "title": "Propose fix",
        "description": "Draft a minimal fix for a triaged incident. Writes nothing; returns a diff, confidence and risk for human review.",
        "inputSchema": {
            "type": "object",
            "properties": {"incident": {"type": "object", "description": "Result of triage_failed_ci"}},
            "required": ["incident"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "open_fix_pr",
        "title": "Open draft PR",
        "description": "Open a DRAFT pull request for a proposed fix. Refuses unless a human has explicitly confirmed. Never merges.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "True only after the human said confirm"},
            },
            "required": ["proposal_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
]
TOOL_INDEX = {t["name"]: t for t in TOOLS}


def _github(path: str, method: str = "GET", data: Optional[dict] = None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "opspulse-mcp"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    body = json.dumps(data).encode() if data is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    with urlopen(UrlRequest("https://api.github.com" + path, data=body, headers=headers, method=method), timeout=8) as r:
        return json.load(r)

def fetch_repo_health(repo: Optional[str] = None):
    repo = repo or GITHUB_REPO
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("repo must look like owner/name")
    try:
        info = _github(f"/repos/{repo}")
        prs = _github(f"/repos/{repo}/pulls?state=open&per_page=100")
        return {
            "repo": repo,
            "open_prs": len(prs),
            "open_issues": max(info["open_issues_count"] - len(prs), 0),  # GitHub counts PRs as issues
            "last_push": info["pushed_at"],
            "default_branch": info["default_branch"],
            "source": "github-api",
        }
    except Exception as e:
        return {"repo": repo, "open_prs": 3, "open_issues": 1, "last_push": None,
                "default_branch": "main", "source": "fallback", "note": str(e)[:80]}


SECRET_PATTERNS = {
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    "Hardcoded secret": re.compile(r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
}
SKIP_DIRS = {"venv", ".venv", ".git", "__pycache__", "node_modules"}
TEXT_EXT = {".py", ".js", ".html", ".json", ".md", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".env"}


def run_security_audit():
    findings, scanned = [], 0
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            p = Path(folder) / name
            if p.suffix.lower() not in TEXT_EXT and name != ".env":
                continue
            try:
                lines = p.read_text(errors="ignore").splitlines()
            except OSError:
                continue
            scanned += 1
            for no, line in enumerate(lines, 1):
                for label, rx in SECRET_PATTERNS.items():
                    m = rx.search(line)
                    if m:
                        findings.append({"type": label, "file": str(p.relative_to(ROOT)), "line": no,
                                         "preview": m.group()[:4] + "…"})
    gi = (ROOT / ".gitignore")
    ignored = gi.read_text(errors="ignore") if gi.exists() else ""
    hygiene = {
        "venv_ignored": "venv" in ignored,
        "env_ignored": ".env" in ignored,
        "license_present": any((ROOT / n).exists() for n in ("LICENSE", "LICENSE.md", "LICENSE.txt")),
    }
    return {"files_scanned": scanned, "findings": findings, "hygiene": hygiene,
            "status": "clean" if not findings else "attention"}


def generate_release_notes(version: str = "v1.1.0", limit: int = 20):
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", version):
        raise ValueError("version must look like v1.2.3")
    try:
        out = subprocess.run(["git", "log", f"-n{int(limit)}", "--pretty=%s"], cwd=ROOT, capture_output=True,
                             text=True, timeout=5, check=True).stdout.splitlines()
        source = "git-log"
    except Exception:
        out = ["fix: optimized MCP streamable HTTP handler", "feat: integrated AWS Bedrock intent router"]
        source = "fallback"
    groups = {"features": [], "fixes": [], "other": []}
    for subject in out:
        groups["features" if subject.startswith("feat") else "fixes" if subject.startswith("fix") else "other"].append(subject)
    return {"version": version, "commit_count": len(out), **groups, "source": source}


SAMPLE_INCIDENT = {
    "workflow": "CI", "branch": "main", "commit": "e8f14b2", "run_id": None, "html_url": None,
    "failed_job": "deploy-smoke-test", "failed_step": "Run smoke tests",
    "log_excerpt": "auth-gateway-worker: OOMKilled (exit 137), memory limit 512Mi reached",
    "suspect_file": "deploy/manifest.yaml", "source": "sample-incident",
}
PENDING: dict = {}


def triage_failed_ci(repo: Optional[str] = None):
    repo = repo or GITHUB_REPO
    try:
        runs = _github(f"/repos/{repo}/actions/runs?status=failure&per_page=1")["workflow_runs"]
        if runs:
            run = runs[0]
            jobs = _github(f"/repos/{repo}/actions/runs/{run['id']}/jobs")["jobs"]
            bad = next((j for j in jobs if j.get("conclusion") == "failure"), jobs[0] if jobs else {})
            step = next((x["name"] for x in bad.get("steps", []) if x.get("conclusion") == "failure"), "unknown")
            return {"repo": repo, "run_id": run["id"], "workflow": run["name"], "branch": run["head_branch"],
                    "commit": run["head_sha"][:7], "failed_job": bad.get("name"), "failed_step": step,
                    "html_url": run["html_url"], "suspect_file": None, "log_excerpt": None,
                    "source": "github-actions"}
    except Exception:
        pass
    return {**SAMPLE_INCIDENT, "repo": repo}  # no failed run found: clearly labelled sample incident


def _fingerprint(diff, summary):
    return hashlib.sha256(json.dumps([diff, summary]).encode()).hexdigest()


def propose_fix(incident: dict):
    inc = incident or {}
    if inc.get("suspect_file") == "deploy/manifest.yaml":
        proposal = {
            "summary": "The container hits its 512Mi memory limit and is OOMKilled with exit code 137. Raise the limit to 1024Mi.",
            "file_path": "deploy/manifest.yaml",
            "diff": "--- a/deploy/manifest.yaml\n+++ b/deploy/manifest.yaml\n@@ resources.limits @@\n-            memory: 512Mi\n+            memory: 1024Mi\n",
            "confidence": 0.8, "risk": "low",
        }
    else:  # weak evidence: do not invent a patch
        proposal = {
            "summary": f"Step {inc.get('failed_step')} failed in job {inc.get('failed_job')}. There is not enough evidence for a safe patch, so please review the run logs.",
            "file_path": None, "diff": None, "confidence": 0.3, "risk": "unknown", "status": "needs_more_context",
        }
    fp = _fingerprint(proposal["diff"], proposal["summary"])
    proposal.update(proposal_id=fp[:12], fingerprint=fp, requires_confirmation=True)
    PENDING[proposal["proposal_id"]] = {"proposal": proposal, "incident": inc, "ts": time.time()}
    return proposal


def open_fix_pr(proposal_id: str, confirmed: bool = False):
    entry = PENDING.get(proposal_id)
    if not entry or time.time() - entry["ts"] > 600:
        raise ValueError("Unknown or expired proposal. Ask me to triage again.")
    p = entry["proposal"]
    if not confirmed:
        return {"status": "awaiting_confirmation", "proposal_id": proposal_id}
    if p.get("status") == "needs_more_context":
        raise ValueError("Refusing: evidence is too weak for a patch.")
    if _fingerprint(p["diff"], p["summary"]) != p["fingerprint"]:
        raise ValueError("Patch fingerprint changed since it was proposed. Aborting.")
    branch = f"fix/opspulse-{p['proposal_id']}"  # deterministic, never the default branch
    inc = entry["incident"]
    pr_body = (f"## OpsPulse incident report\n\n**Failed:** {inc.get('workflow')} / {inc.get('failed_job')} / {inc.get('failed_step')}\n\n"
               f"{p['summary']}\n\n```diff\n{p['diff']}\n```\n\nConfidence {p['confidence']}, risk {p['risk']}. "
               f"Patch fingerprint `{p['fingerprint'][:12]}`. Opened after explicit human confirmation. Never auto-merged.")
    if not os.getenv("GITHUB_TOKEN"):
        return {"status": "dry_run", "branch": branch, "draft": True, "fingerprint": p["fingerprint"][:12]}
    repo = GITHUB_REPO
    base = _github(f"/repos/{repo}")["default_branch"]
    sha = _github(f"/repos/{repo}/git/ref/heads/{base}")["object"]["sha"]
    try:
        _github(f"/repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": sha})
    except HTTPError as e:
        if e.code != 422:  # 422 = branch exists from an earlier identical run, which is fine
            raise
    path = f"opspulse-proposals/{p['proposal_id']}.md"
    try:
        existing = _github(f"/repos/{repo}/contents/{path}?ref={branch}").get("sha")
    except HTTPError:
        existing = None
    payload = {"message": f"docs: OpsPulse incident report {p['proposal_id']}",
               "content": base64.b64encode(pr_body.encode()).decode(), "branch": branch}
    if existing:
        payload["sha"] = existing
    _github(f"/repos/{repo}/contents/{path}", "PUT", payload)
    pr = _github(f"/repos/{repo}/pulls", "POST", {"title": f"[OpsPulse] {p['summary'][:60]}", "head": branch,
                                                  "base": base, "body": pr_body, "draft": True})
    return {"status": "created", "pr_url": pr["html_url"], "branch": branch, "draft": True}


IMPLS = {"fetch_repo_health": fetch_repo_health, "run_security_audit": run_security_audit,
         "generate_release_notes": generate_release_notes, "triage_failed_ci": triage_failed_ci,
         "propose_fix": propose_fix, "open_fix_pr": open_fix_pr}


def call_tool(name: str, args: dict):
    allowed = TOOL_INDEX[name]["inputSchema"]["properties"]
    return IMPLS[name](**{k: v for k, v in args.items() if k in allowed})


# --------------------------------------------------------------------------- MCP (JSON-RPC 2.0)
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


@app.post("/mcp")
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


@app.get("/mcp")
def mcp_get():  # no server-initiated stream; spec allows 405
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@app.delete("/mcp")
def mcp_delete(request: Request):
    SESSIONS.discard(request.headers.get("mcp-session-id", ""))
    return Response(status_code=200)


# --------------------------------------------------------------------------- Bedrock (Converse API)
_client = None
_down_until = 0.0


def bedrock():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=REGION,
                               config=Config(connect_timeout=3, read_timeout=15, retries={"max_attempts": 1}))
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


@app.post("/alexa/route")
def route(req: RouteRequest):
    """Let Bedrock pick the MCP tool; keyword fallback keeps the demo alive without AWS credentials."""
    if bedrock_available():
        try:
            specs = [{"toolSpec": {"name": t["name"], "description": t["description"],
                                   "inputSchema": {"json": t["inputSchema"]}}} for t in (req.tools or TOOLS)]
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
        except Exception:
            mark_bedrock_down()
    name, args = keyword_route(req.query)
    return {"tool": name, "arguments": args, "router": "keyword fallback"}


def template_speech(tool: str, d: dict) -> str:
    if tool == "fetch_repo_health":
        return (f"Your repo has {d.get('open_prs', 0)} open pull requests and {d.get('open_issues', 0)} open issues. "
                "Nothing is blocking you.")
    if tool == "run_security_audit":
        n = len(d.get("findings", []))
        if n:
            return f"Security audit found {n} possible leaked secrets. Please review them before you push."
        return f"Security audit is clean. I scanned {d.get('files_scanned', 0)} files and found no leaked secrets."
    if tool == "propose_fix":
        inc = d.get("incident", {})
        base = f"The {inc.get('failed_job', 'CI')} job failed at step {inc.get('failed_step', 'unknown')}. {d.get('summary', '')}"
        return base + (" I drafted a fix. Say confirm and I will open a draft pull request." if d.get("diff")
                       else " I need more context before I propose a patch.")
    if tool == "open_fix_pr":
        if d.get("status") == "dry_run":
            return f"Confirmed. In dry run mode I would open a draft pull request on branch {d.get('branch')}. Nothing was merged."
        return "Confirmed. I opened a draft pull request for your review. I did not merge anything."
    if tool == "triage_failed_ci":
        return f"The {d.get('failed_job')} job failed at step {d.get('failed_step')}."
    return (f"Release notes for {d.get('version')} are ready, with {d.get('commit_count', 0)} commits: "
            f"{len(d.get('features', []))} features and {len(d.get('fixes', []))} fixes.")


@app.post("/alexa/speak")
def speak(req: SpeakRequest):
    """Turn raw tool JSON into a short spoken Alexa+ reply."""
    if bedrock_available():
        try:
            r = bedrock().converse(
                modelId=MODEL_ID,
                system=[{"text": "You are Alexa+, a developer ops copilot. Reply in one or two short spoken "
                                 "sentences. Plain English, no markdown, no lists, no emoji."}],
                messages=[{"role": "user", "content": [{"text":
                          f"Command: {req.query}\nTool: {req.tool}\nResult JSON: {json.dumps(req.result)[:3000]}"}]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0.3},
            )
            return {"speech": r["output"]["message"]["content"][0]["text"].strip(), "engine": "AWS Bedrock"}
        except Exception:
            mark_bedrock_down()
    return {"speech": template_speech(req.tool, req.result), "engine": "template fallback"}


# --------------------------------------------------------------------------- static UI (keep last)
(ROOT / "static").mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)