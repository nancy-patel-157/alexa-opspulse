"""Settings shared by every module. Override via environment variables."""
import os
from pathlib import Path

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")  # no slow IMDS lookups on a laptop

ROOT = Path(__file__).resolve().parent.parent  # project root (scanned by the security audit)
SERVER_INFO = {"name": "opspulse-mcp", "title": "OpsPulse Dev Ops Copilot", "version": "1.2.0"}
SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
GITHUB_REPO = os.getenv("GITHUB_REPO", "nancy-patel-157/alexa-opspulse")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
REGION = os.getenv("AWS_REGION", "us-east-1")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
