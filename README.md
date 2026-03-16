# MixBox

MixBox is an AI-powered art therapy companion that makes the creative process reflective and interactive. Users choose colors and shapes from a preset canvas palette — and from those choices, Mix (a warm voice AI) asks questions about what drew them there, listens to their response, and generates collage images that reflect their emotional state.

Art is already therapeutic. MixBox makes it dynamic by turning every creative decision into a conversation.

---

## How it works

1. **Session starts** — Mix greets the user with a single breath exercise, then invites them to choose
2. **User selects a color or shape** — e.g. clicks "blue" from the color palette
3. **Mix responds** — *"Blue. What's drawing you there right now?"* — and waits
4. **User responds** — via voice, in their own time
5. **Mix offers to generate** — *"I could turn that into something for your canvas."*
6. **User confirms** — Mix creates a collage image through Imagen 3, which lands in the palette and on the canvas
7. **User arranges** — drag, rotate, and compose the generated images into a personal collage
8. **Session closes** — Mix witnesses what was made and offers a genuine, specific reflection

---

## Architecture

Two services, both on Cloud Run:

```
Browser  ──WebSocket──►  mixbox-main  (FastAPI + Gemini Live / ADK)
                               │
                               └──HTTP (A2A)──►  mixbox-architect  (ADK + Imagen 3)
```

**`mixbox-main`** — the core server. Handles the WebSocket bidi-stream with the browser, runs Mix (Gemini Live via Google ADK), detects canvas action events and offer/confirmation patterns, and coordinates image generation.

**`mixbox-architect`** — a separate A2A agent service. Receives structured image prompts from the main service and generates collage images via Imagen 3 (`imagen-3.0-generate-002`). Runs as an independent Cloud Run service so image generation never blocks the voice stream.

---

## Project structure

```
backend/
├── main.py                    # FastAPI server, WebSocket, ADK session management
├── index.html                 # Single-file frontend (Fabric.js canvas, color/shape picker)
├── ambient.mp3                # Background ambient audio
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Container for mixbox-main
├── deploy.sh                  # Deploy both services to Cloud Run
│
├── mix_agent/
│   ├── agent.py               # Mix — Gemini Live LlmAgent + system prompt
│   └── __init__.py
│
└── image_architect/
    ├── agent.py               # Image Architect LlmAgent + Imagen tool
    ├── client.py              # HTTP client: calls Architect from main.py
    ├── server.py              # A2A FastAPI server for the Architect
    ├── requirements.txt       # Architect-specific dependencies
    └── Dockerfile             # Container for mixbox-architect
```

---

## Local development

### Prerequisites

- Python 3.11+
- Google Cloud project with Vertex AI and Imagen APIs enabled
- `gcloud` CLI authenticated (`gcloud auth application-default login`)

### Environment variables

Create a `.env` file in `backend/` (never commit this):

```env
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
MODEL_ID=gemini-live-2.5-flash-preview-native-audio-09-2025
ARCHITECT_URL=http://localhost:8081
KEEPALIVE_INTERVAL=8
```

### Run locally

In two separate terminals:

**Terminal 1 — Image Architect:**
```bash
cd image_architect
pip install -r requirements.txt
python server.py
# Listening on http://localhost:8081
```

**Terminal 2 — Main service:**
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
# Open http://localhost:8080
```

---

## Deployment

### Quick deploy (main service only)

Use this when you've only changed `main.py`, `mix_agent/`, or `index.html`:

```bash
gcloud run deploy mixbox-main \
  --source . \
  --region us-central1 \
  --quiet
```

### Full deploy (both services)

Use this when you've changed anything in `image_architect/`, or for a fresh deployment:

```bash
chmod +x deploy.sh
./deploy.sh
```

The script deploys the Architect first, captures its URL, then passes it as `ARCHITECT_URL` to the main service automatically.

### Required GCP APIs

The deploy script enables these automatically, but for reference:

- `run.googleapis.com`
- `artifactregistry.googleapis.com`
- `cloudbuild.googleapis.com`
- `aiplatform.googleapis.com` (for Vertex AI + Imagen)

---

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | `mixbox2026` | GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Region for Vertex AI and Cloud Run |
| `MODEL_ID` | `gemini-live-2.5-flash-preview-native-audio-09-2025` | Gemini Live model for Mix |
| `ARCHITECT_MODEL_ID` | `gemini-2.5-flash` | LLM for the Architect agent |
| `ARCHITECT_URL` | `http://localhost:8081` | Internal URL of the Architect service |
| `KEEPALIVE_INTERVAL` | `8` | Seconds between audio keepalive frames |

---

## Canvas interaction model

The left sidebar has three sections:

- **Choose a color** — 12 color swatches (blue, teal, green, yellow, orange, red, pink, purple, brown, black, white, gold)
- **Or a shape** — 7 shape buttons (circle, square, triangle, star, spiral, wave, cloud)
- **Your palette** — generated images that accumulate through the session; drag to canvas or click to place

Selecting a color or shape sends a `canvas_action` WebSocket message to the server, which injects a context hint to Mix. Mix responds with a warm observation and one question, waits for the user's verbal answer, then offers to generate an image. User confirms by voice ("yes", "sure", "go ahead", etc.) and the image is created.

---

## Reproducible Testing

### Requirements

- Python 3.11+
- A Google Cloud project with the following APIs enabled:
  - Vertex AI (`aiplatform.googleapis.com`)
  - Imagen (`imagegeneration.googleapis.com`)
  - Cloud Run (`run.googleapis.com`)
- `gcloud` CLI authenticated: `gcloud auth application-default login`
- A browser with microphone access (Chrome recommended)

### Option A — Run locally (fastest)

**Step 1: Clone and configure**

```bash
git clone https://github.com/pranitchand/mixbox.git
cd mixbox/backend
cp .env.example .env
# Edit .env and set GOOGLE_CLOUD_PROJECT to your GCP project ID
```

**Step 2: Start the Image Architect service**

```bash
cd image_architect
pip install -r requirements.txt
python server.py
# Running on http://localhost:8081
```

**Step 3: Start the main service**

```bash
cd ..
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

**Step 4: Open the app**

Navigate to `http://localhost:8080` in Chrome. Grant microphone and camera permissions when prompted.

---

### Option B — Deploy to Cloud Run

```bash
chmod +x deploy.sh
./deploy.sh
```

The script deploys the Architect first, captures its URL, then injects it as `ARCHITECT_URL` into the main service. Both services will be live on Cloud Run within ~3 minutes. The final URL is printed at the end of the script.

---

### End-to-end test flow

Once the app is running, walk through this sequence to verify all systems:

1. **Click "start session"** — Mix should greet you within 2–3 seconds: *"Hi... how are you feeling today?"*
2. **Respond verbally** — say anything; Mix should respond to what you actually said
3. **Pick a color** from the palette — Mix should acknowledge it with a warm 4–6 word observation
4. **Hold a hand gesture** in front of the camera (fist, point, peace sign, or pinch) for 1.5 seconds — a stamp mark should appear on the canvas
5. **Click the done button** (or say *"I'm done"*) — Mix should say *"I'll create something for you"* and a spinner should appear
6. **Wait ~10–15 seconds** — a generated image should snap into the first jigsaw slot on the canvas
7. **Mix should respond** to the image with *"How does it feel... to look at that?"*
8. **Talk to Mix** about anything — verify it responds conversationally, not with short fragments

---

### What to check if something isn't working

| Symptom | Likely cause |
|---|---|
| Mix doesn't speak at session start | Gemini Live model not available in your region — check `GOOGLE_CLOUD_LOCATION` |
| Image never generates | `ARCHITECT_URL` not set correctly, or Imagen API not enabled |
| Microphone not captured | Browser permissions denied — check site settings |
| Hand tracking not showing | Camera permissions denied, or MediaPipe CDN failed to load |
| Mix reads instructions aloud | Model version mismatch — verify `MODEL_ID` in `.env` |

---

## Tech stack

- **Frontend** — React + Fabric.js (interactive canvas), MediaPipe Hands, WebSocket audio streaming
- **Voice AI** — Gemini Live 2.5 Flash (Native Audio) via [Google ADK](https://google.github.io/adk-docs/) bidi-streaming
- **Image generation** — Imagen 3 via Vertex AI
- **Backend** — FastAPI + uvicorn on Cloud Run
- **Agent-to-agent** — Google ADK A2A protocol between main and architect services
