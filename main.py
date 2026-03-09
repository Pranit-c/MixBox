"""
MixBox — Main Server
main.py

FastAPI + WebSocket server using ADK bidi-streaming pattern.

ARCHITECTURE:
- Mix handles voice + canvas observation via Gemini Live bidi-streaming
- User moves through a gentle ritual: breath check-in → color → shape → image → reflection
- Shape pick auto-triggers image generation (no gesture needed)
- Mix holds the space between steps with warm, unhurried guidance

Message types from browser:
  bytes                → raw PCM audio (16kHz, 16-bit)
  { type: "image" }   → JPEG canvas frame (every 2s)
  { type: "canvas_action", action_type, value } → color or shape pick
  { type: "session_close" }                     → user ending session
  { type: "ping" }                              → keepalive no-op

Run:
    source venv/bin/activate
    uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""

import asyncio
import base64
import json
import logging
import os

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
from image_architect.client import generate_gesture_image

# ── Voice keyword maps for auto-selecting color / shape ───────────────────────
VOICE_COLORS = {
    'blue': '#4a7fa5', 'teal': '#3d8c8c', 'green': '#4a7c59',
    'yellow': '#dbb84a', 'orange': '#d4783c', 'red': '#c24a3c',
    'pink': '#cc7a90', 'purple': '#7c5cbf', 'brown': '#7c5a3c',
    'black': '#2a2520', 'white': '#f5f0e8', 'gold': '#c9a840',
}
VOICE_SHAPES = {'circle', 'square', 'triangle', 'star', 'spiral', 'wave', 'cloud'}

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
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name="Aoede"   # warm, natural female voice
            )
        )
    ),
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

    # ── Flow state — tracks the creative ritual ────────────────────────────────
    # breath + check-in → color → shape → image → reflection
    flow_state = {
        "color":            "",     # color name picked
        "shape":            "",     # shape name picked
        "voice_transcript": "",     # accumulated user speech (texture + anything said)
        "generating":       False,  # True while image is in-flight
    }

    # Wake Mix
    logger.info("Sending Hello stimulus to wake Mix…")
    live_request_queue.send_content(
        types.Content(parts=[types.Part(text="Hello")])
    )

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

                # ── Raw PCM audio ─────────────────────────────────────────────
                if "bytes" in message:
                    live_request_queue.send_realtime(
                        types.Blob(
                            mime_type="audio/pcm;rate=16000",
                            data=message["bytes"],
                        )
                    )

                elif "text" in message:
                    try:
                        data     = json.loads(message["text"])
                        msg_type = data.get("type")

                        # ── Canvas JPEG frame → Mix sees the canvas ───────────
                        if msg_type == "image":
                            image_bytes = base64.b64decode(data["data"])
                            live_request_queue.send_realtime(
                                types.Blob(
                                    mime_type="image/jpeg",
                                    data=image_bytes,
                                )
                            )

                        # ── Color / shape picked ──────────────────────────────
                        elif msg_type == "canvas_action":
                            action_type  = data.get("action_type", "")
                            action_value = data.get("value", "")
                            logger.info(f"Canvas action: {action_type}={action_value}")

                            if action_type == "color":
                                # Store color, reset downstream state
                                # Mix will acknowledge + invite shape (guided by system prompt)
                                flow_state["color"]            = action_value
                                flow_state["shape"]            = ""

                                hint = (
                                    f"[CANVAS ACTION: User selected color '{action_value}'. "
                                    f"Acknowledge with one soft observation — 3 to 6 words. "
                                    f"Then gently ask: 'Is there a shape that wants to join that?' "
                                    f"Stop. Wait. Say nothing more.]"
                                )

                            elif action_type == "shape":
                                # Store shape — auto-trigger image generation now
                                flow_state["shape"] = action_value
                                color = flow_state["color"]
                                voice = flow_state["voice_transcript"]  # includes texture check-in

                                logger.info(
                                    f"Shape selected → generating: color={color!r}, "
                                    f"shape={action_value!r}, voice={voice[:60]!r}"
                                )

                                hint = (
                                    f"[CANVAS ACTION: User selected shape '{action_value}' "
                                    f"with color '{color}'. Texture context from their words: '{voice}'. "
                                    f"Notice the color and shape together in one warm, brief sentence. "
                                    f"Then say: 'Let me make something from that.' "
                                    f"Stop. Wait quietly until the image appears. Say nothing more.]"
                                )

                                # Auto-trigger image generation (guard against double-fire)
                                if not flow_state["generating"]:
                                    flow_state["generating"] = True

                                    try:
                                        await ws.send_text(json.dumps({
                                            "type": "generating_canvas_image"
                                        }))
                                    except Exception:
                                        pass

                                    async def on_image_ready(img_b64: str):
                                        flow_state["generating"] = False
                                        try:
                                            await ws.send_text(json.dumps({
                                                "type":   "generated_image",
                                                "data":   img_b64,
                                                "source": "ritual",
                                            }))
                                            logger.info("Ritual image sent to browser.")
                                        except Exception as e:
                                            logger.warning(f"Could not send image: {e}")

                                        # After image lands, invite reflection
                                        try:
                                            live_request_queue.send_content(
                                                types.Content(parts=[types.Part(text=(
                                                    "[IMAGE CREATED: The image has appeared on the canvas. "
                                                    "After a quiet moment, gently ask: 'How does it feel to look at that?' "
                                                    "Wait for their response. Then softly offer: "
                                                    "'Would you like to sit with this... or is there something new that wants to come?' "
                                                    "Wait. Do not rush.]"
                                                ))])
                                            )
                                        except Exception:
                                            pass

                                    asyncio.create_task(
                                        generate_gesture_image(
                                            color=color,
                                            shape=action_value,
                                            gesture_intensity="medium",   # texture/voice carries the mood
                                            voice_transcript=voice,
                                            on_image_ready=on_image_ready,
                                        )
                                    )

                            else:
                                hint = None

                            if hint:
                                try:
                                    live_request_queue.send_content(
                                        types.Content(parts=[types.Part(text=hint)])
                                    )
                                except Exception as e:
                                    logger.warning(f"Canvas action hint failed: {e}")

                        # ── Session close ─────────────────────────────────────
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
                # ── Audio / image inline data ──────────────────────────────────
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

                # ── Mix transcript → browser ───────────────────────────────────
                if (
                    hasattr(event, "output_audio_transcription")
                    and event.output_audio_transcription
                ):
                    transcript = event.output_audio_transcription
                    await ws.send_text(json.dumps({"transcript": transcript}))

                # ── User speech → accumulate + detect color/shape keywords ────────
                if (
                    hasattr(event, "input_audio_transcription")
                    and event.input_audio_transcription
                ):
                    user_text = event.input_audio_transcription
                    logger.info(f"User said: {user_text}")
                    flow_state["voice_transcript"] = (
                        flow_state["voice_transcript"] + " " + user_text
                    ).strip()[-400:]

                    # Tokenise — strip punctuation, lowercase
                    words = [
                        w.strip(".,!?\"'—-").lower()
                        for w in user_text.split()
                    ]

                    # ── Auto-select color if user speaks one ──────────────────
                    if not flow_state["color"] and not flow_state["generating"]:
                        for word in words:
                            if word in VOICE_COLORS:
                                flow_state["color"] = word
                                logger.info(f"Voice color detected: {word}")
                                try:
                                    await ws.send_text(json.dumps({
                                        "type": "voice_select",
                                        "kind": "color",
                                        "value": word,
                                    }))
                                    live_request_queue.send_content(
                                        types.Content(parts=[types.Part(text=(
                                            f"[VOICE: User chose the color '{word}' by speaking. "
                                            f"Acknowledge with 2–4 words only. Stop.]"
                                        ))])
                                    )
                                except Exception as e:
                                    logger.warning(f"Voice color select error: {e}")
                                break

                    # ── Auto-select shape if color is set and user speaks one ─
                    elif (
                        flow_state["color"]
                        and not flow_state["shape"]
                        and not flow_state["generating"]
                    ):
                        for word in words:
                            if word in VOICE_SHAPES:
                                flow_state["shape"] = word
                                flow_state["generating"] = True
                                logger.info(f"Voice shape detected: {word}")
                                try:
                                    await ws.send_text(json.dumps({
                                        "type": "voice_select",
                                        "kind": "shape",
                                        "value": word,
                                    }))
                                    await ws.send_text(json.dumps({
                                        "type": "generating_canvas_image"
                                    }))
                                    live_request_queue.send_content(
                                        types.Content(parts=[types.Part(text=(
                                            f"[VOICE: User chose shape '{word}' with color "
                                            f"'{flow_state['color']}'. "
                                            f"Say exactly: 'Let me make something from that.' "
                                            f"Then silence. Wait.]"
                                        ))])
                                    )
                                except Exception as e:
                                    logger.warning(f"Voice shape select error: {e}")

                                color_val = flow_state["color"]
                                voice_val  = flow_state["voice_transcript"]

                                async def on_voice_image_ready(img_b64: str):
                                    flow_state["generating"] = False
                                    try:
                                        await ws.send_text(json.dumps({
                                            "type":   "generated_image",
                                            "data":   img_b64,
                                            "source": "ritual",
                                        }))
                                        logger.info("Voice-selected image sent.")
                                    except Exception as e:
                                        logger.warning(f"Could not send voice image: {e}")
                                    try:
                                        live_request_queue.send_content(
                                            types.Content(parts=[types.Part(text=(
                                                "[IMAGE CREATED: Image is on the canvas. "
                                                "After a quiet moment, say: 'How does it feel to look at that?' "
                                                "Then wait. Do not rush.]"
                                            ))])
                                        )
                                    except Exception:
                                        pass

                                asyncio.create_task(
                                    generate_gesture_image(
                                        color=color_val,
                                        shape=word,
                                        gesture_intensity="medium",
                                        voice_transcript=voice_val,
                                        on_image_ready=on_voice_image_ready,
                                    )
                                )
                                break

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
