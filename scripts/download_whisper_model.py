#!/usr/bin/env python3
"""
Download a faster-whisper model from Hugging Face to a local directory.
Used at Docker build time to bundle the model in the image (no runtime download on Cloud Run).

Usage:
  python scripts/download_whisper_model.py [model_name] [output_dir]
  Default: model_name=tiny, output_dir=/app/whisper_models

Example:
  python scripts/download_whisper_model.py tiny /app/whisper_models
"""
import os
import sys

# Map TRANSCRIPTION_MODEL style names to Hugging Face repo IDs (Systran faster-whisper)
HF_MODEL_IDS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "distil-large-v3": "Systran/faster-whisper-large-v3",  # or distil if exists; same ID often used
}


def main() -> None:
    model_name = (sys.argv[1] if len(sys.argv) > 1 else "tiny").strip().lower()
    base_dir = (sys.argv[2] if len(sys.argv) > 2 else "/app/whisper_models").rstrip("/")
    out_dir = f"{base_dir}/{model_name}"

    repo_id = HF_MODEL_IDS.get(model_name)
    if not repo_id:
        print(f"Unknown model: {model_name}. Choose one of: {list(HF_MODEL_IDS.keys())}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(out_dir, exist_ok=True)
    print(f"Downloading {repo_id} -> {out_dir}")

    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=repo_id,
            local_dir=out_dir,
            local_dir_use_symlinks=False,
        )
        print(f"Done. Model is in {out_dir}")
    except Exception as e:
        print(f"Download failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
