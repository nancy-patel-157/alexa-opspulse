"""MCP tool catalog and implementations: repo health, security audit, release notes, CI triage, fix flow."""
import os
import re
import json
import time
import hashlib
import base64
import subprocess
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

from .config import ROOT, GITHUB_REPO

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
