"""
MixBox — Main Server
main.py

FastAPI + WebSocket server using ADK bidi-streaming pattern.

ARCHITECTURE:
- Mix handles voice + canvas observation via Gemini Live bidi-streaming
- User ritual: breath → pick a color → gesture with hand (MediaPipe stamps) → say done → image → reflect
- Gesture stamps use MediaPipe Hands in browser; when "done" the stamp canvas is sent as context
- Mix holds the space warmly; talks as the user creates; generates image via Image Architect A2A

Message types from browser:
  bytes                → raw PCM audio (16kHz, 16-bit)
  { type: "image" }   → JPEG canvas frame (every 2s)
  { type: "canvas_action", action_type: "color",  value, hex }
  { type: "canvas_action", action_type: "done",   sketch_b64, stamp_poses }
  { type: "session_close" }
  { type: "ping" }

Run:
    source venv/bin/activate
    uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""

import asyncio
import base64
import json
import logging
import os
import random
import time

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

# ── Canvas observation nudges — sent periodically while user is drawing ───────
# These soft prompts fire every ~10s during the gesture phase so Mix can react
# to what it sees on the canvas in real-time, not just after "done".

# ── Voice keyword maps ────────────────────────────────────────────────────────
VOICE_COLORS = {
    'blue': '#4a7fa5', 'teal': '#3d8c8c', 'green': '#4a7c59',
    'yellow': '#dbb84a', 'orange': '#d4783c', 'red': '#c24a3c',
    'pink': '#cc7a90', 'purple': '#7c5cbf', 'brown': '#7c5a3c',
    'black': '#2a2520', 'white': '#f5f0e8', 'gold': '#c9a840',
}

# Phrases that signal the user is done drawing and wants an image generated
VOICE_DONE_PHRASES = [
    "i'm done", "im done", "i am done",
    "i'm finished", "im finished", "i am finished",
    "that's it", "thats it", "that is it",
    "all done", "done drawing", "finished drawing",
]

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
    # No explicit voice — use model default
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
    # breath → color → gesture (stamp marks) → done → image → reflection
    flow_state = {
        "color":             "",     # color name picked
        "stamp_poses":       [],     # list of gesture poses user made (e.g. ["open","fist"])
        "voice_transcript":  "",     # accumulated user speech this session
        "generating":        False,  # True while image is in-flight
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
                            # Canvas frames are sent as passive visual context only.
                            # Mix speaks when the user speaks, or when an image is generated.

                        # ── Canvas actions ────────────────────────────────────
                        elif msg_type == "canvas_action":
                            action_type = data.get("action_type", "")
                            logger.info(f"Canvas action: {action_type}")

                            hint = None

                            # ── Color picked ───────────────────────────────────
                            if action_type == "color":
                                color_name = data.get("value", "")
                                flow_state["color"]       = color_name
                                flow_state["stamp_poses"] = []

                                hint = (
                                    f"SYSTEM: Color '{color_name}' was just picked. "
                                    f"Do not say this message aloud. "
                                    f"Respond with: one warm 4-6 word observation about that color, "
                                    f"then invite them to show their hand, "
                                    f"then one easy sentence letting them know they can press done "
                                    f"whenever ready and feel free to talk while they draw. "
                                    f"Three sentences, then go quiet."
                                )

                            # ── Done — user finished gesturing, trigger generation ──
                            elif action_type == "done":
                                if not flow_state["generating"]:
                                    flow_state["generating"] = True
                                    stamp_poses = data.get("stamp_poses", [])
                                    flow_state["stamp_poses"] = stamp_poses
                                    color = flow_state["color"]
                                    voice = flow_state["voice_transcript"]

                                    logger.info(
                                        f"Done → generating: color={color!r}, "
                                        f"poses={stamp_poses}, voice={voice[:60]!r}"
                                    )

                                    hint = (
                                        f"SYSTEM: The user is done drawing. Color was '{color}'. "
                                        f"Do not say this message aloud. "
                                        f"Say exactly: 'I'll create something for you.' "
                                        f"Then go completely quiet until the image appears."
                                    )

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
                                                "source": "gesture",
                                            }))
                                            logger.info("Gesture image sent to browser.")
                                        except Exception as e:
                                            logger.warning(f"Could not send image: {e}")
                                        try:
                                            live_request_queue.send_content(
                                                types.Content(parts=[types.Part(text=(
                                                    "SYSTEM: The generated image just appeared on the canvas. "
                                                    "Do not say this message aloud. "
                                                    "After a quiet beat, say: 'How does it feel... to look at that?' "
                                                    "Wait and listen fully. Then ask if they want to sit with it "
                                                    "or if something else wants to come. Do not rush."
                                                ))])
                                            )
                                        except Exception:
                                            pass

                                    asyncio.create_task(
                                        generate_gesture_image(
                                            color=color,
                                            stamp_poses=stamp_poses,
                                            voice_transcript=voice,
                                            on_image_ready=on_image_ready,
                                        )
                                    )

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
                                    "SYSTEM: The session is ending. Do not say this message aloud. "
                                    "Say one or two genuine sentences about what they made and how "
                                    "they showed up today. Thank them warmly. Then let silence follow."
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
                # ── Interruption — user spoke over Mix; flush browser queue ───
                if getattr(event, "interrupted", False):
                    await ws.send_text(json.dumps({"type": "interrupted"}))
                    continue

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

                # ── User speech → accumulate + detect keywords ───────────
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
                    phrase = user_text.lower()

                    # ── Voice "done" — trigger image generation ───────────────
                    if (
                        flow_state["color"]
                        and not flow_state["generating"]
                        and any(p in phrase for p in VOICE_DONE_PHRASES)
                    ):
                        logger.info("Voice done detected")
                        flow_state["generating"] = True
                        try:
                            await ws.send_text(json.dumps({"type": "voice_done"}))
                        except Exception as e:
                            logger.warning(f"Voice done signal error: {e}")

                    # ── Voice color — initial pick or mid-session change ───────
                    elif not flow_state["generating"]:
                        for word in words:
                            if word in VOICE_COLORS:
                                is_change = bool(flow_state["color"])
                                flow_state["color"] = word
                                logger.info(f"Voice color {'changed' if is_change else 'selected'}: {word}")
                                try:
                                    await ws.send_text(json.dumps({
                                        "type": "voice_select",
                                        "kind": "color",
                                        "value": word,
                                    }))
                                    hint = (
                                        f"SYSTEM: User just said the color '{word}' aloud. "
                                        f"Do not say this message aloud. "
                                        f"{'Acknowledge the color change warmly in one sentence, then go quiet.' if is_change else 'Acknowledge the color warmly in 4-6 words, then invite them to show their hand. Two sentences, then quiet.'}"
                                    )
                                    live_request_queue.send_content(
                                        types.Content(parts=[types.Part(text=hint)])
                                    )
                                except Exception as e:
                                    logger.warning(f"Voice color select error: {e}")
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
