# Cloud Run Deploy

From the repo root:

```bash
./scripts/deploy-cloudrun.sh
```

The script builds Cloud Run env vars from local `.env`, excludes local secrets
from source upload, and deploys the container.

For local Twilio testing, run the app and expose it with ngrok:

```bash
python main.py
ngrok http 5050
```

Set the Twilio Voice webhook to:

```text
https://YOUR_HOST/incoming-call
```
