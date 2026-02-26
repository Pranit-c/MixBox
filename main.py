"""
MixBox — Main Server
main.py

FastAPI + WebSocket server using ADK bidi-streaming pattern.

ARCHITECTURE:
- Mix handles voice + canvas observation
- Canvas frames streamed so Mix can see the collage being built
- Canvas action (color/shape selection) injected as system hint to Mix
- Mix asks one question, waits for response, offers to generate an image
- User confirms → Image Architect generates → image lands in palette

Run:
    source venv/bin/activate
    uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""

import asyncio
import base64
import json
import logging
import os
import re

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from google.adk.agents import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from mix_agent.agent import root_agent
from image_architect.client import generate_canvas_action_image, generate_single_image

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mixbox")

APP_NAME   = "mixbox"
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "mixbox2026")
LOCATION   = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

os.environ["GOOGLE_CLOUD_PROJECT"]      = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"]     = LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "8"))

# Phrases Mix uses when offering to generate an image
OFFER_PATTERNS = [
    "would you like me to create something from that",
    "shall i make an image of that",
    "i could turn that into something",
    "let me create that for you now",
    "create something from that",
    "make an image of that",
    "turn that into",
]

# User confirmation phrases
CONFIRM_PATTERNS = [
    "yes", "yeah", "sure", "please", "go ahead",
    "do it", "create it", "make it", "i'd like that",
    "that would be nice", "okay", "ok", "absolutely",
]

# ── ADK Session + Runner ──────────────────────────────────────────────────────

session_service = InMemorySessionService()
runner = Runner(
    app_name=APP_NAME,
    agent=root_agent,
    session_service=session_service,
)

# ── RunConfig ─────────────────────────────────────────────────────────────────

run_config = RunConfig(
    streaming_mode=StreamingMode.BIDI,
    response_modalities=["AUDIO"],
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    session_resumption=types.SessionResumptionConfig(),
)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI()
app.mount("/static", StaticFiles(directory="."), name="static")

@app.get("/")
async def root():
    with open("index.html") as f:
        return HTMLResponse(f.read())


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    logger.info("Browser connected.")

    user_id  = "user"
    session  = await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
    )
    session_id = session.id
    logger.info(f"ADK session created: {session_id}")

    live_request_queue = LiveRequestQueue()
    session_alive = {"value": True}

    # Offer state — tracks pending image generation offers from Mix
    offer_state = {
        "pending":        False,
        "mix_transcript": "",   # what Mix said when offering
        "cooldown":       False,
        # Most recent canvas action (color or shape the user selected)
        "canvas_action": {
            "type":          "",   # "color" or "shape"
            "value":         "",   # e.g. "blue", "circle"
            "user_response": "",   # user's verbal response after Mix's question
        },
    }

    # Wake Mix with just "Hello" → triggers breath + canvas invitation
    logger.info("Sending Hello stimulus to wake Mix…")
    live_request_queue.send_content(
        types.Content(parts=[types.Part(text="Hello")])
    )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def detect_offer(transcript: str) -> bool:
        t = transcript.lower()
        return any(pattern in t for pattern in OFFER_PATTERNS)

    def detect_confirmation(transcript: str) -> bool:
        t = transcript.lower().strip()
        return any(pattern in t for pattern in CONFIRM_PATTERNS)

    def extract_image_prompt(mix_transcript: str) -> str:
        """Extract a visual prompt from what Mix observed on the canvas."""
        t = mix_transcript
        for pattern in OFFER_PATTERNS:
            t = re.sub(pattern, "", t, flags=re.IGNORECASE)
        t = t.strip(" .?!")
        if len(t) < 10:
            return "layered textures, expressive marks, painterly collage"
        return f"{t}, expressive collage texture, painterly"

    # ── Keepalive ─────────────────────────────────────────────────────────────
    async def keepalive_task():
        silent_frame = bytes(160)
        while session_alive["value"]:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not session_alive["value"]:
                break
            try:
                live_request_queue.send_realtime(
                    types.Blob(mime_type="audio/pcm;rate=16000", data=silent_frame)
                )
            except Exception as e:
                logger.warning(f"Keepalive failed: {e}")
                break

    # ── Upstream task ─────────────────────────────────────────────────────────
    async def upstream_task():
        try:
            while True:
                message = await ws.receive()

                if "bytes" in message:
                    live_request_queue.send_realtime(
                        types.Blob(
                            mime_type="audio/pcm;rate=16000",
                            data=message["bytes"],
                        )
                    )

                elif "text" in message:
                    try:
                        data = json.loads(message["text"])
                        msg_type = data.get("type")

                        if msg_type == "image":
                            # Canvas frame — Mix watches the collage being built
                            image_bytes = base64.b64decode(data["data"])
                            live_request_queue.send_realtime(
                                types.Blob(
                                    mime_type="image/jpeg",
                                    data=image_bytes,
                                )
                            )

                        elif msg_type == "canvas_action":
                            # User selected a color or shape from the picker
                            action_type  = data.get("action_type", "")
                            action_value = data.get("value", "")

                            logger.info(f"Canvas action: {action_type}={action_value}")

                            # Store in offer state and reset user response
                            offer_state["canvas_action"]["type"]          = action_type
                            offer_state["canvas_action"]["value"]         = action_value
                            offer_state["canvas_action"]["user_response"] = ""
                            offer_state["pending"]                        = False

                            # Inject context hint so Mix knows what was chosen
                            hint = (
                                f"[CANVAS ACTION: User selected the {action_type} '{action_value}'. "
                                f"Acknowledge their choice warmly with one short observation. "
                                f"Then ask one gentle, open question about what drew them to it. "
                                f"After they respond, offer to create a collage image from their answer. "
                                f"Wait after each sentence.]"
                            )
                            try:
                                live_request_queue.send_content(
                                    types.Content(parts=[types.Part(text=hint)])
                                )
                            except Exception as e:
                                logger.warning(f"Canvas action hint failed: {e}")

                        elif msg_type == "session_close":
                            logger.info("Session close signal received.")
                            live_request_queue.send_content(
                                types.Content(parts=[types.Part(text=(
                                    "The user is ending the session now. "
                                    "Witness the final canvas. "
                                    "Say something true and specific about what they made "
                                    "and how they were present. "
                                    "Thank them genuinely. Keep it short. Let silence follow."
                                ))])
                            )

                        elif msg_type == "ping":
                            pass

                    except json.JSONDecodeError:
                        pass

        except WebSocketDisconnect:
            logger.info("Browser disconnected.")
            session_alive["value"] = False
            live_request_queue.close()
        except Exception as e:
            logger.error(f"Upstream error: {e}")
            session_alive["value"] = False
            live_request_queue.close()

    # ── Downstream task ───────────────────────────────────────────────────────
    async def downstream_task():
        try:
            async for event in runner.run_live(
                user_id=user_id,
                session_id=session_id,
                live_request_queue=live_request_queue,
                run_config=run_config,
            ):
                # Route inline_data by mime_type
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "inline_data") and part.inline_data:
                            mime = part.inline_data.mime_type or ""
                            if mime.startswith("audio/"):
                                await ws.send_bytes(part.inline_data.data)
                            elif mime.startswith("image/"):
                                img_b64 = base64.b64encode(
                                    part.inline_data.data
                                ).decode("utf-8")
                                await ws.send_text(json.dumps({
                                    "type": "generated_image",
                                    "data": img_b64,
                                }))

                # Mix transcription → browser + offer detection
                if (
                    hasattr(event, "output_audio_transcription")
                    and event.output_audio_transcription
                ):
                    transcript = event.output_audio_transcription
                    await ws.send_text(json.dumps({"transcript": transcript}))

                    if detect_offer(transcript) and not offer_state["cooldown"]:
                        offer_state["pending"]        = True
                        offer_state["mix_transcript"] = transcript
                        logger.info(f"Offer detected: {transcript[:80]}")

                # User transcription → accumulate response + detect confirmation
                if (
                    hasattr(event, "input_audio_transcription")
                    and event.input_audio_transcription
                ):
                    user_text = event.input_audio_transcription
                    logger.info(f"User said: {user_text}")

                    # Accumulate user's response to the canvas action question
                    if offer_state["canvas_action"]["type"]:
                        ca = offer_state["canvas_action"]
                        ca["user_response"] = (ca["user_response"] + " " + user_text).strip()

                    # Detect confirmation
                    if (
                        offer_state["pending"]
                        and detect_confirmation(user_text)
                        and not offer_state["cooldown"]
                    ):
                        logger.info("User confirmed image generation.")
                        offer_state["pending"] = False
                        offer_state["cooldown"] = True

                        ca = offer_state["canvas_action"]

                        # Notify browser that we're generating
                        try:
                            await ws.send_text(json.dumps({
                                "type": "generating_canvas_image"
                            }))
                        except Exception:
                            pass

                        async def on_image_ready(img_b64: str):
                            try:
                                await ws.send_text(json.dumps({
                                    "type": "generated_image",
                                    "data": img_b64,
                                    "source": "canvas_action",
                                }))
                                logger.info("Generated image forwarded to browser.")
                            except Exception as e:
                                logger.warning(f"Could not forward image: {e}")

                        if ca["type"]:
                            # Canvas action — use color/shape + user response
                            logger.info(
                                f"Generating from canvas action: "
                                f"{ca['type']}={ca['value']}, "
                                f"response='{ca['user_response'][:60]}'"
                            )
                            asyncio.create_task(
                                generate_canvas_action_image(
                                    action_type=ca["type"],
                                    action_value=ca["value"],
                                    user_response=ca["user_response"],
                                    on_image_ready=on_image_ready,
                                )
                            )
                        else:
                            # Canvas observation offer — extract from Mix's transcript
                            prompt = extract_image_prompt(offer_state["mix_transcript"])
                            logger.info(f"Generating from observation: {prompt[:60]}")
                            asyncio.create_task(
                                generate_single_image(
                                    prompt=prompt,
                                    mood_tone="reflective",
                                    on_image_ready=on_image_ready,
                                )
                            )

                        async def reset_cooldown():
                            await asyncio.sleep(30)
                            offer_state["cooldown"] = False
                            offer_state["pending"]  = False

                        asyncio.create_task(reset_cooldown())

        except Exception as e:
            logger.error(f"Downstream error: {e}")
            session_alive["value"] = False
            try:
                await ws.send_text(json.dumps({"error": str(e)}))
            except Exception:
                pass

    # ── Run three tasks concurrently ──────────────────────────────────────────
    await asyncio.gather(
        upstream_task(),
        downstream_task(),
        keepalive_task(),
    )
