"""
MixBox — Image Architect Agent
image_architect/agent.py

Remote A2A agent. Receives structured prompts from Mix and returns
Imagen-generated collage images as base64 JPEGs.

Mix calls this as a tool — never directly by the user.
"""

import os
import base64
import logging
import random

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google import genai
from google.genai import types as genai_types

logger = logging.getLogger("mixbox.architect")

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "mixbox2026")
LOCATION   = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
MODEL_ID   = os.environ.get("ARCHITECT_MODEL_ID", "gemini-2.5-flash")

# ── Imagen client ─────────────────────────────────────────────────────────────

_imagen_client = None

def _get_imagen_client():
    global _imagen_client
    if _imagen_client is None:
        _imagen_client = genai.Client(
            vertexai=True,
            project=PROJECT_ID,
            location=LOCATION,
        )
    return _imagen_client

# ── Image generation tool ─────────────────────────────────────────────────────

def generate_collage_image(
    prompt: str,
    mood_tone: str = "reflective",
    style: str = "torn paper collage, mixed media, expressive, non-photorealistic",
) -> dict:
    """
    Generate a collage image using Imagen.

    Args:
        prompt: Descriptive prompt for the image. Should reflect the user's
                emotional state and collage themes (e.g. "soft fog over still water,
                muted blues and grays, fragmented texture").
        mood_tone: Emotional register — e.g. 'reflective', 'expansive', 'tender',
                   'unsettled', 'grounded'. Used to shape the prompt.
        style: Visual style guidance. Default is painterly collage texture.

    Returns:
        dict with 'image_b64' (base64 JPEG string) and 'prompt_used'.
    """
    client = _get_imagen_client()

    # Build a rich, therapeutically-attuned prompt
    full_prompt = (
        f"{prompt}. "
        f"Mood: {mood_tone}. "
        "Style: torn paper collage, mixed media art, visible brushstrokes, "
        "impasto texture, layered fragments, watercolor washes, "
        "aged paper grain, expressive mark-making, non-photorealistic, "
        "art therapy aesthetic, painterly and tactile. "
        "NOT photorealistic. NOT a photograph. NOT CGI. "
        "Evocative and abstract — fragments of feeling, not illustrations. "
        "No text, no people, no faces."
    )

    aspect_ratio = random.choice(["3:4", "4:3", "16:9", "9:16"])
    logger.info(f"Generating image: {full_prompt[:80]}… [ratio={aspect_ratio}]")

    try:
        response = client.models.generate_images(
            model="imagen-3.0-generate-002",
            prompt=full_prompt,
            config=genai_types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=aspect_ratio,
                safety_filter_level="block_few",
                person_generation="dont_allow",
            ),
        )

        if not response.generated_images:
            logger.error("Imagen returned no images")
            return {"error": "No image generated", "prompt_used": full_prompt}

        img_bytes = response.generated_images[0].image.image_bytes
        img_b64   = base64.b64encode(img_bytes).decode("utf-8")

        logger.info("Image generated successfully")
        return {
            "image_b64": img_b64,
            "prompt_used": full_prompt,
        }

    except Exception as e:
        logger.error(f"Imagen error: {e}")
        return {"error": str(e), "prompt_used": full_prompt}


# ── ADK Tool wrapper ──────────────────────────────────────────────────────────

generate_image_tool = FunctionTool(func=generate_collage_image)

# ── Image Architect LlmAgent ──────────────────────────────────────────────────

ARCHITECT_SYSTEM_PROMPT = """
You are the Image Architect for MixBox — a specialized image generation agent.

You receive requests from Mix (the user-facing companion) containing:
- An emotional prompt describing what the user needs
- A mood tone (e.g. reflective, expansive, tender, unsettled, grounded)
- Optional style guidance

Your job:
1. Call generate_collage_image with the provided parameters
2. Return the result directly — image_b64 and prompt_used

You generate images that are:
- Evocative and non-literal (fragments of feeling, not illustrations)
- Suitable for collage — textures, abstractions, landscapes, light, color fields
- Never featuring faces, people, or text
- Painterly, layered, and emotionally resonant

You do not communicate with the user. You only serve Mix.
""".strip()

root_agent = LlmAgent(
    name="image_architect",
    model=MODEL_ID,
    description="Generates emotionally-tuned collage images for MixBox sessions via Imagen.",
    instruction=ARCHITECT_SYSTEM_PROMPT,
    tools=[generate_image_tool],
)