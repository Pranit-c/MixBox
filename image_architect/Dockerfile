# MixBox — Image Architect Service
# Runs A2A server for image generation via Imagen

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Cloud Run sets PORT env var — default to 8081
ENV PORT=8081
ENV PYTHONUNBUFFERED=1
ENV HOST_URL=0.0.0.0
ENV PROTOCOL=https
ENV A2A_PORT=8081

# Expose port
EXPOSE 8081

# Run the A2A server
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT}