# OpsPulse - Alexa+ Autonomous MCP Agent

OpsPulse is a simulated Alexa+ voice copilot built for developers, utilizing the latest Streamable HTTP Model Context Protocol (MCP) spec (2025-11-25) and AWS Bedrock for intelligent action synthesis.

## Features
- **Alexa+ Voice/Chat Interface**: Direct conversational triage for dev operations.
- **MCP Tool Protocol**: Self-hosted HTTP endpoints handling local repo audits and release automations.
- **AWS Bedrock Synthesis**: Contextual translation of raw system logs into crisp conversational audio responses.

## Tech Stack
- **Backend**: Python, FastAPI, Uvicorn, Boto3 (AWS Bedrock)
- **Frontend**: Lightweight vanilla JS & CSS dashboard
- **Protocol**: Model Context Protocol (Spec 2025-11-25, Streamable HTTP)

## Setup & Run
1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt` (or `pip install fastapi uvicorn boto3 pydantic python-dotenv`)
3. Launch server: `uvicorn main:app --reload --port 8000`
4. Access interface at `http://localhost:8000`

## License
MIT