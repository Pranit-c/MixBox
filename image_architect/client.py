"""
MixBox — Image Architect Direct Client
image_architect/client.py

Calls the Image Architect A2A server directly via HTTP from main.py.
This decouples image generation from Mix's bidi-stream.

Public functions:
- generate_gesture_image()  — image from color + shape + gesture + voice (primary path)
- generate_single_image()   — 1 image from canvas observation on demand (fallback)
"""

import json
import logging
import os
import httpx

logger = logging.getLogger("mixbox.architect_client")

ARCHITECT_URL = os.environ.get("ARCHITECT_URL", "http://localhost:8081")

# ── Color → visual prompt map ─────────────────────────────────────────────────

COLOR_PROMPT_MAP = {
    "blue":      "deep ocean surface, layered blues, cool depths, painterly wash",
    "deep blue": "midnight sky over still water, indigo and navy, vast and quiet",
    "teal":      "shallow tidal pools, blue-green water over stone, translucent light",
    "green":     "forest undergrowth after rain, deep greens and shadow, living texture",
    "yellow":    "morning light flooding open field, warm gold, expansive and still",
    "orange":    "late afternoon embers and ochre, burnt warmth, fading glow",
    "red":       "deep red rose petals on dark stone, saturated warmth, contained intensity",
    "pink":      "cherry blossom petals on pale linen, soft pink, tender and fleeting",
    "purple":    "twilight over mountains, deep violet and lavender, between states",
    "brown":     "warm earth and clay, roots pressing into soil, grounded and ancient",
    "black":     "ink spreading through water, deep black, absorption and depth",
    "white":     "fresh snow on bare branches, white silence, clean and still",
    "gold":      "candlelight on old wood, amber warmth, present and glowing",
}

# ── Gesture pose → visual quality ─────────────────────────────────────────────

POSE_PROMPT_MAP = {
    "open":  "radiating outward, expansive, open and spreading, light emanating from center",
    "fist":  "dense and contained, concentrated energy, enclosed, held and heavy",
    "point": "directed and focused, single intention, sharp and reaching toward something",
    "peace": "branching and diverging, two paths, forked and connected, dual nature",
    "pinch": "delicate and intimate, small and precise, fine detail, a single point of light",
}

# ── Mood tone keywords → Imagen prompt enrichment ────────────────────────────

MOOD_PROMPT_MAP = {
    "heavy":      "dense storm clouds pressing low over dark water, heavy grey texture",
    "scattered":  "dried leaves scattered across pale stone, fragmented light, autumn",
    "hopeful":    "first light through morning fog, soft gold, quiet and still",
    "tired":      "late evening light through curtains, soft shadows, still air",
    "anxious":    "tangled roots beneath forest floor, fine dark lines, intricate",
    "calm":       "smooth river stones under shallow water, cool blues and greys",
    "sad":        "rain on a window, blurred city lights beyond, cool tones",
    "angry":      "cracked dry earth, deep reds and ochres, rough texture",
    "warm":       "late afternoon sun on old stone walls, golden warmth",
    "rough":      "cracked earth after drought, weathered texture, warm ochre",
    "smooth":     "still water at dawn, mirror surface, soft light",
    "light":      "white flower petals on pale linen, soft and delicate",
    "dark":       "forest at dusk, deep greens and blacks, mysterious depth",
    "fragmented": "shattered glass fragments, light refracting, abstract",
    "searching":  "open horizon at sea, vast sky, small figure distant",
    "grounded":   "tree roots gripping earth, strong and deep, warm browns",
    "floating":   "clouds from above, aerial view, soft whites and greys",
    "open":       "open palm with light falling across it, releasing",
    "reaching":   "branches reaching toward light, upward movement, hopeful",
    "still":      "mirror lake at dawn, perfect stillness, suspended breath",
    "present":    "single candle flame, steady and warm, here and now",
    "water":      "deep ocean surface, layered blues, cool depths, painterly wash",
    "fire":       "ember glow at dusk, deep reds and oranges, heat and light",
    "earth":      "warm earth and clay, roots pressing into soil, grounded and ancient",
    "air":        "morning mist over open field, soft whites and greys, drifting",
}

DEFAULT_PROMPTS = [
    "soft fog over still water, muted blues and grays, fragmented texture, painterly",
    "dried autumn leaves on pale stone, warm ochres, quiet and still",
    "first light through morning curtains, soft gold, gentle shadows",
]


# ── Internal: A2A call ────────────────────────────────────────────────────────

async def _call_architect(prompt: str, mood_tone: str, request_id: str) -> str | None:
    """
    Core function — makes a single A2A call to the Image Architect.
    Returns base64 image string or None on failure.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{
                    "text": (
                        f"Generate a collage image with these parameters:\n"
                        f"prompt: {prompt}\n"
                        f"mood_tone: {mood_tone}\n"
                        f"style: painterly collage, textured, layered"
                    )
                }],
                "messageId": f"msg-{request_id}",
            }
        }
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.post(ARCHITECT_URL, json=payload)
            if response.status_code == 200:
                result = response.json()
                return _extract_image_from_a2a_response(result)
            else:
                logger.error(f"Architect returned {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Architect call failed: {e}")
            return None


# ── Public: gesture image (primary generation path) ───────────────────────────

async def generate_gesture_image(
    color: str,
    stamp_poses: list,
    voice_transcript: str,
    on_image_ready,
):
    """
    Generate an image from the gesture collage ritual:
    color + hand gesture poses + voice context.

    color:            e.g. "blue"
    stamp_poses:      list of pose names used, e.g. ["open", "fist", "peace"]
    voice_transcript: what the user said while creating
    on_image_ready:   async callback(base64_str)
    """
    # Base visual from color
    color_base = COLOR_PROMPT_MAP.get(color.lower(), DEFAULT_PROMPTS[0]) if color else DEFAULT_PROMPTS[0]

    # Build gesture quality from unique poses (preserve order, deduplicate)
    seen = set()
    unique_poses = [p for p in stamp_poses if not (p in seen or seen.add(p))]
    pose_qualities = [POSE_PROMPT_MAP[p] for p in unique_poses if p in POSE_PROMPT_MAP]
    gesture_str = ", ".join(pose_qualities) if pose_qualities else "expressive, present, flowing"

    # Detect mood from voice transcript
    mood_tone = "reflective"
    voice_lower = voice_transcript.lower() if voice_transcript else ""
    for keyword in MOOD_PROMPT_MAP:
        if keyword in voice_lower:
            mood_tone = keyword
            break

    # Build combined prompt
    parts = [p for p in [color_base, gesture_str] if p]
    if voice_transcript and len(voice_transcript.strip()) > 5:
        parts.append(voice_transcript.strip()[:120])
    prompt = ", ".join(parts)

    logger.info(
        f"Gesture image — color={color!r}, poses={unique_poses}, mood={mood_tone}"
    )
    logger.info(f"Prompt: {prompt[:140]}…")

    img_b64 = await _call_architect(
        prompt=prompt,
        mood_tone=mood_tone,
        request_id=f"gesture-{color}-{'-'.join(unique_poses[:3])}-{abs(hash(voice_transcript))}",
    )
    if img_b64:
        await on_image_ready(img_b64)
        logger.info("Gesture image forwarded to browser.")
    else:
        logger.warning("Gesture image: no image data returned.")


# ── Public: single on-demand image (fallback / canvas observation) ────────────

async def generate_single_image(
    prompt: str,
    mood_tone: str,
    on_image_ready,
):
    """
    Generate a single image from a freeform prompt.
    Used for canvas observation offers or any ad-hoc generation.
    Calls on_image_ready(base64_str) when complete.
    """
    logger.info(f"Generating single image: {prompt[:60]}…")
    img_b64 = await _call_architect(
        prompt=prompt,
        mood_tone=mood_tone,
        request_id=f"observation-{abs(hash(prompt))}",
    )
    if img_b64:
        await on_image_ready(img_b64)
        logger.info("Single image forwarded to browser.")
    else:
        logger.warning("Single image: no image data returned.")


# ── Internal: A2A response parser ─────────────────────────────────────────────

def _extract_image_from_a2a_response(response: dict) -> str | None:
    """
    Extract base64 image data from an A2A JSON-RPC response.
    Tries multiple paths in order of likelihood.
    """
    try:
        result = response.get("result", {})

        # Primary path: history messages with function response
        history = result.get("history", [])
        for message in history:
            for part in message.get("parts", []):
                if part.get("kind") == "data":
                    data = part.get("data", {})
                    if "image_b64" in data:
                        return data["image_b64"]
                    if "response" in data and isinstance(data["response"], dict):
                        if "image_b64" in data["response"]:
                            return data["response"]["image_b64"]

        # Fallback: artifacts
        artifacts = result.get("artifacts", [])
        for artifact in artifacts:
            for part in artifact.get("parts", []):
                if part.get("kind") == "data":
                    data = part.get("data", {})
                    if "image_b64" in data:
                        return data["image_b64"]
                    if "response" in data and "image_b64" in data.get("response", {}):
                        return data["response"]["image_b64"]

        # Fallback: status message with embedded JSON
        message = result.get("status", {}).get("message", {})
        for part in message.get("parts", []):
            if part.get("kind") == "text":
                try:
                    text_data = json.loads(part.get("text", "{}"))
                    if "image_b64" in text_data:
                        return text_data["image_b64"]
                except Exception:
                    pass

        return None

    except Exception as e:
        logger.warning(f"Could not extract image from A2A response: {e}")
        return None
