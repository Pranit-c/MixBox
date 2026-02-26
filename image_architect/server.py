"""
MixBox — Image Architect A2A Server
image_architect/server.py

Run locally:
    python server.py

Environment variables (.env):
    GOOGLE_CLOUD_PROJECT   = mixbox2026
    GOOGLE_CLOUD_LOCATION  = us-central1
    A2A_PORT               = 8081
    HOST_URL               = localhost
    PROTOCOL               = http
"""

import logging
import os
import uvicorn
from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from agent import root_agent

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mixbox.architect.server")

# ── Everything that changes between environments lives here ───────────────────
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "mixbox2026")
LOCATION   = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
HOST       = os.environ.get("HOST_URL", "localhost")
PROTOCOL   = os.environ.get("PROTOCOL", "http")
PORT       = int(os.environ.get("A2A_PORT", "8081"))

os.environ["GOOGLE_CLOUD_PROJECT"]      = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"]     = LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# ── A2A App ───────────────────────────────────────────────────────────────────
app = to_a2a(root_agent, host=HOST, port=PORT, protocol=PROTOCOL)

logger.info(f"Image Architect A2A server ready → {PROTOCOL}://{HOST}:{PORT}")

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, log_level="info")