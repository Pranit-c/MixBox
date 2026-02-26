#!/bin/bash
# MixBox — Deploy Both Services to Cloud Run
# Run from ~/mixbox/backend/
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh

set -e  # Exit on any error

PROJECT_ID="mixbox2026"
REGION="us-central1"
MAIN_SERVICE="mixbox-main"
ARCHITECT_SERVICE="mixbox-architect"

echo "🚀 Deploying MixBox to Cloud Run..."
echo "Project: $PROJECT_ID | Region: $REGION"
echo ""

# ── Step 1: Set project ───────────────────────────────────────────────────────
gcloud config set project $PROJECT_ID
gcloud config set run/region $REGION

# ── Step 2: Enable required APIs ─────────────────────────────────────────────
echo "✅ Enabling required APIs..."
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    aiplatform.googleapis.com \
    --quiet

# ── Step 3: Deploy Image Architect first ──────────────────────────────────────
echo ""
echo "📦 Deploying Image Architect service..."
cd image_architect

gcloud run deploy $ARCHITECT_SERVICE \
    --source . \
    --region $REGION \
    --platform managed \
    --allow-unauthenticated \
    --memory 2Gi \
    --cpu 2 \
    --timeout 300 \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,GOOGLE_GENAI_USE_VERTEXAI=True,PROTOCOL=https,A2A_PORT=8080" \
    --quiet

# Get the Architect URL
ARCHITECT_URL=$(gcloud run services describe $ARCHITECT_SERVICE \
    --region $REGION \
    --format 'value(status.url)')

echo "✅ Image Architect deployed: $ARCHITECT_URL"
cd ..

# ── Step 4: Deploy Mix main service ──────────────────────────────────────────
echo ""
echo "🎙️  Deploying Mix main service..."

gcloud run deploy $MAIN_SERVICE \
    --source . \
    --region $REGION \
    --platform managed \
    --allow-unauthenticated \
    --memory 2Gi \
    --cpu 2 \
    --timeout 3600 \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,GOOGLE_GENAI_USE_VERTEXAI=True,ARCHITECT_URL=$ARCHITECT_URL,KEEPALIVE_INTERVAL=8" \
    --quiet

# Get the main service URL
MAIN_URL=$(gcloud run services describe $MAIN_SERVICE \
    --region $REGION \
    --format 'value(status.url)')

echo ""
echo "✅ Mix deployed: $MAIN_URL"
echo ""
echo "🎉 MixBox is live!"
echo ""
echo "Main service:       $MAIN_URL"
echo "Image Architect:    $ARCHITECT_URL"
echo ""
echo "Open your browser: $MAIN_URL"