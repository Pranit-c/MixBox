"""
MixBox — Main Server
main.py

FastAPI + WebSocket server using ADK bidi-streaming pattern.

ARCHITECTURE:
- Mix handles voice + canvas observation via Gemini Live bidi-streaming
- User completes a three-step creative ritual: color → shape → gesture/voice
- Gesture message triggers image generation combining all three inputs
- Mix holds the space between steps with warm, brief guidance

Message types from browser:
  bytes                → raw PCM audio (16kHz, 16-bit)
  { type: "image" }   → JPEG canvas frame (every 2s)
  { type: "canvas_action", action_type, value } → color or shape pick
  { type: "gesture", intensity }                → motion detected after shape pick
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

    # ── Flow state — tracks the three-step creative ritual ────────────────────
    # color → shape → gesture/voice → image
    flow_state = {
        "color":            "",     # color name picked in step 1
        "shape":            "",     # shape name picked in step 2
        "voice_transcript": "",     # accumulated speech during gesture window
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

                        # ── Step 1 or 2: color / shape picked ─────────────────
                        elif msg_type == "canvas_action":
                            action_type  = data.get("action_type", "")
                            action_value = data.get("value", "")
                            logger.info(f"Canvas action: {action_type}={action_value}")

                            if action_type == "color":
                                # Step 1 — store color, reset downstream state,
                                # ask Mix to prompt user for a shape
                                flow_state["color"]            = action_value
                                flow_state["shape"]            = ""
                                flow_state["voice_transcript"] = ""

                                hint = (
                                    f"[CANVAS ACTION: User selected color '{action_value}'. "
                                    f"Acknowledge warmly with 2–5 words. "
                                    f"Then say: 'Now — pick a shape to go with it.' "
                                    f"Stop. Wait. Say nothing more.]"
                                )

                            elif action_type == "shape":
                                # Step 2 — store shape, reset voice window,
                                # ask Mix to open the gesture/voice window
                                flow_state["shape"]            = action_value
                                flow_state["voice_transcript"] = ""
                                color = flow_state["color"]

                                hint = (
                                    f"[CANVAS ACTION: User selected shape '{action_value}' "
                                    f"with color '{color}'. "
                                    f"Acknowledge the combination briefly — one sentence. "
                                    f"Then say: 'Now — move, speak, or both. I'm watching and listening.' "
                                    f"Stop completely. The gesture window is open. Say nothing more.]"
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

                        # ── Step 3: gesture received → generate image ──────────
                        elif msg_type == "gesture":
                            gesture_intensity = data.get("intensity", "medium")
                            color = flow_state["color"]
                            shape = flow_state["shape"]
                            voice = flow_state["voice_transcript"]

                            logger.info(
                                f"Gesture: intensity={gesture_intensity}, "
                                f"color={color!r}, shape={shape!r}, "
                                f"voice={voice[:60]!r}"
                            )

                            # Tell Mix what was received so she can acknowledge
                            hint = (
                                f"[GESTURE: color='{color}', shape='{shape}', "
                                f"motion='{gesture_intensity}', voice='{voice}'. "
                                f"Acknowledge what you noticed — the motion, what they said, or both. "
                                f"One sentence, warm and specific. "
                                f"Then say exactly: 'Let me create something from that now.' "
                                f"Wait quietly until the image arrives.]"
                            )
                            try:
                                live_request_queue.send_content(
                                    types.Content(parts=[types.Part(text=hint)])
                                )
                            except Exception as e:
                                logger.warning(f"Gesture hint failed: {e}")

                            # Generate image (guard against double-fire)
                            if not flow_state["generating"]:
                                flow_state["generating"] = True

                                try:
                                    await ws.send_text(json.dumps({
                                        "type": "generating_canvas_image"
                                    }))
                                except Exception:
                                    pass

                                async def on_gesture_ready(img_b64: str):
                                    flow_state["generating"]       = False
                                    flow_state["voice_transcript"] = ""
                                    try:
                                        await ws.send_text(json.dumps({
                                            "type":   "generated_image",
                                            "data":   img_b64,
                                            "source": "gesture",
                                        }))
                                        logger.info("Gesture image sent to browser.")
                                    except Exception as e:
                                        logger.warning(f"Could not send gesture image: {e}")

                                asyncio.create_task(
                                    generate_gesture_image(
                                        color=color,
                                        shape=shape,
                                        gesture_intensity=gesture_intensity,
                                        voice_transcript=voice,
                                        on_image_ready=on_gesture_ready,
                                    )
                                )

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

                # ── User speech → accumulate for gesture context ───────────────
                # Voice compounds naturally with the gesture when it fires.
                # Keeps last 300 chars so the prompt stays focused.
                if (
                    hasattr(event, "input_audio_transcription")
                    and event.input_audio_transcription
                ):
                    user_text = event.input_audio_transcription
                    logger.info(f"User said: {user_text}")
                    flow_state["voice_transcript"] = (
                        flow_state["voice_transcript"] + " " + user_text
                    ).strip()[-300:]

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
