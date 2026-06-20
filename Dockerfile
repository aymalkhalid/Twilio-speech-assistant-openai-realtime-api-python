# Voice Agent Starter - OpenAI Realtime + Twilio (GCP Cloud Run / any container host)
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY main.py config.py system_instructions.py ./
COPY prompts/ ./prompts/
COPY services/ ./services/
COPY static/ ./static/
COPY scripts/ ./scripts/

# Bundle Whisper model in image so Cloud Run does not download from hub at runtime (avoids 502 on first request). Default tiny for lower RAM/CPU.
ARG WHISPER_MODEL_NAME=tiny
RUN python scripts/download_whisper_model.py "${WHISPER_MODEL_NAME}" /app/whisper_models
ENV WHISPER_MODEL_PATH=/app/whisper_models/${WHISPER_MODEL_NAME}

# Cloud Run sets PORT at runtime; config.py reads PORT from env
ENV PORT=8080
EXPOSE 8080

# Exec form: uvicorn runs as PID 1 so it receives SIGTERM for graceful shutdown (Cloud Run). PORT is set at runtime.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
