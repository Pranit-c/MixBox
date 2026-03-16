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
CANVAS_NUDGES = [
    (
        "[CANVAS: Look at the canvas right now. You can see the full collage — "
        "any images already placed in the puzzle, and the marks being made now. "
        "If something in what you see moves you, say one present-tense observation. "
        "You can reference what's already there AND what's being added. "
        "One short sentence, or stay quiet. No completion language.]"
    ),
    (
        "[CANVAS: You're watching the collage grow. Look at what's already in the puzzle "
        "and what the person is creating right now. If a color, a shape, or a connection "
        "between the pieces speaks to you, say one quiet thing. Present tense only. "
        "Or stay silent and hold the space.]"
    ),
    (
        "[CANVAS: The collage is taking shape — piece by piece. Look at the whole canvas: "
        "what's been made, what's being made now. If something catches you — a recurring color, "
        "a mood, a quality — say one soft thing about what you notice. Or say nothing.]"
    ),
    (
        "[CANVAS: Look at what's there. The puzzle is filling in. "
        "If the image you see — the pieces placed, the marks being added — moves you, "
        "reflect one thing back simply and gently. Stay in the present moment. Or hold the quiet.]"
    ),
]
CANVAS_NUDGE_INTERVAL = 10.0  # seconds between nudges

# ── Voice keyword maps ────────────────────────────────────────────────────────
VOICE_COLORS = {
    'blue': '#4a7fa5', 'teal': '#3d8c8c', 'green': '#4a7c59',
    'yellow': '#dbb84a', 'orange': '#d4783c', 'red': '#c24a3c',
    'pink': '#cc7a90', 'purple': '#7c5cbf', 'brown': '#7c5a3c',
    'black': '#2a2520', 'white': '#f5f0e8', 'gold': '#c9a840',
}

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
    # breath → color → gesture (stamp marks) → done → image → reflection
    flow_state = {
        "color":             "",     # color name picked
        "stamp_poses":       [],     # list of gesture poses user made (e.g. ["open","fist"])
        "voice_transcript":  "",     # accumulated user speech this session
        "generating":        False,  # True while image is in-flight
        "last_canvas_nudge": 0.0,    # monotonic time of last canvas observation nudge
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
                            # While the user is actively drawing, nudge Mix every
                            # ~10s so it reacts to the canvas in real-time rather
                            # than only responding when explicitly triggered.
                            if (
                                flow_state["color"]
                                and not flow_state["generating"]
                                and (time.monotonic() - flow_state["last_canvas_nudge"])
                                    > CANVAS_NUDGE_INTERVAL
                            ):
                                flow_state["last_canvas_nudge"] = time.monotonic()
                                try:
                                    live_request_queue.send_content(
                                        types.Content(parts=[types.Part(text=random.choice(CANVAS_NUDGES))])
                                    )
                                except Exception as e:
                                    logger.warning(f"Canvas nudge failed: {e}")

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
                                    f"[CANVAS ACTION: User chose color '{color_name}'. "
                                    f"Acknowledge with a warm 4–6 word observation about the color. "
                                    f"Then invite them to show their hand. "
                                    f"Then — in one easy, unhurried sentence — let them know they can "
                                    f"press the done button whenever they're ready, and that they're "
                                    f"welcome to talk to you while they draw. "
                                    f"Three sentences total. Then go quiet and watch.]"
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
                                        f"[USER SAID DONE: They've finished making gesture marks. "
                                        f"Color: '{color}'. Gestures used: {stamp_poses}. "
                                        f"Say exactly: 'Let me make something from that.' "
                                        f"Then go completely quiet. Wait until the image appears.]"
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
                                                    "[IMAGE CREATED: The image has appeared on the canvas. "
                                                    "After a quiet beat, ask: 'How does it feel... to look at that?' "
                                                    "Wait. Listen fully. Then: "
                                                    "'Would you like to sit with this... or is there something else that wants to come?' "
                                                    "Wait. Do not rush.]"
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

                    # ── Auto-select color by voice (before color is set) ──────
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
                                            f"[VOICE: User chose color '{word}' by speaking. "
                                            f"Acknowledge warmly in 4–6 words. "
                                            f"Then invite them to show their hand. "
                                            f"Two sentences total. Then quiet.]"
                                        ))])
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
