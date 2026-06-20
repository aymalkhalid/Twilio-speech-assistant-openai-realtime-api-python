#!/usr/bin/env bash
# Deploy to Cloud Run from source, loading env vars from .env (so the container can start).
# Aligned with config.py: all vars are passed except those in skip (file paths / Cloud Run-set).
# Usage: from project root, run:  ./scripts/deploy-cloudrun.sh
# Requires: gcloud CLI, project set (gcloud config set project YOUR_PROJECT_ID).

set -e
cd "$(dirname "$0")/.."
ENV_FILE="${1:-.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE. Create it or pass a path: ./scripts/deploy-cloudrun.sh /path/to/.env"
  exit 1
fi

# Build a temporary Cloud Run env-vars file from .env. Skip: file paths and PORT (set by Cloud Run).
CLOUD_RUN_ENV=$(mktemp)
trap 'rm -f "$CLOUD_RUN_ENV"' EXIT
export ENV_FILE CLOUD_RUN_ENV
python3 << 'PYEOF'
import os
# Skip vars that are local file paths or overridden by Cloud Run. All others (Supabase, CALL_RECORD_BACKEND, Twilio, recording, etc.) are passed.
# GOOGLE_CALENDAR_CREDENTIALS_JSON and GOOGLE_APPLICATION_CREDENTIALS: skipped so a local path in .env is not sent to Cloud Run. For booking on GCP, set them in Cloud Run (Variables & Secrets): (1) path to a mounted secret file, or (2) reference a secret containing the full JSON (app supports inline JSON). See docs/booking/GCP_BOOKING_TROUBLESHOOTING.md.
skip = {"GOOGLE_CALENDAR_CREDENTIALS_JSON", "GOOGLE_APPLICATION_CREDENTIALS", "PORT"}
out = {}
with open(os.environ["ENV_FILE"]) as f:
    for line in f:
        line = line.split("#")[0].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in skip:
            continue
        out[k] = v
with open(os.environ["CLOUD_RUN_ENV"], "w") as f:
    for k, v in out.items():
        v_esc = v.replace("\\", "\\\\").replace('"', '\\"')
        f.write(f'{k}: "{v_esc}"\n')
PYEOF

echo "Deploying to Cloud Run (env vars from $ENV_FILE)..."
# Inject Google Calendar credentials from Secret Manager (secret name must match: google-calendar-credentials).
# GOOGLE_CALENDAR_ID should be set in .env so it is passed via --env-vars-file.
DEPLOY_OUTPUT=$(gcloud run deploy speech-assistant \
  --source . \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --timeout 3600 \
  --env-vars-file "$CLOUD_RUN_ENV" \
  --set-secrets=GOOGLE_CALENDAR_CREDENTIALS_JSON=google-calendar-credentials:latest 2>&1)
echo "$DEPLOY_OUTPUT"
# Use the same Service URL that gcloud deploy prints (deterministic format); status.url can differ (legacy format).
SVC_URL=$(echo "$DEPLOY_OUTPUT" | sed -n 's/.*Service URL: \(https:\/\/[^[:space:]]*\).*/\1/p')
if [[ -z "$SVC_URL" ]]; then
  SVC_URL=$(gcloud run services describe speech-assistant --region us-central1 --format='value(status.url)' 2>/dev/null || true)
fi
echo "  Twilio voice webhook: ${SVC_URL}/incoming-call"
echo "  If using call recording, set RECORDING_STATUS_CALLBACK_BASE_URL=${SVC_URL} in .env and redeploy."
