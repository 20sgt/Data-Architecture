#!/usr/bin/env python3
"""
Create transcripts for podcast MP3 files stored in GCS using local Whisper.

This uses faster-whisper on your machine (or any free local runtime).
It does NOT call Google Cloud Speech-to-Text (no STT API charges).

New Whisper transcripts are written to a separate GCS prefix so existing
transcripts under podcasts/transcripts/ are never read or overwritten.

Usage:
  ./.venv/bin/python3 transcribe.py
  ./.venv/bin/python3 transcribe.py --limit 1
  ./run_transcribe.sh --limit 1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from ingest import AUDIO_PREFIX, get_storage_client, load_config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OLD PATH (commented out on purpose)
# Earlier Google STT + mixed Whisper transcripts live here. Do not write to
# this prefix so those files stay undisturbed.
# TRANSCRIPT_PREFIX = "podcasts/transcripts"
# ---------------------------------------------------------------------------

# NEW PATH: Whisper-only, from-scratch corpus. Skip/exists checks use only this
# prefix, so podcasts/transcripts/ is never touched.
TRANSCRIPT_PREFIX = os.getenv(
    "TRANSCRIPT_PREFIX",
    "podcasts/transcripts_whisper",
)
LEGACY_TRANSCRIPT_PREFIX = "podcasts/transcripts"

DEFAULT_LANGUAGE_CODE = os.getenv("TRANSCRIPTION_LANGUAGE_CODE", "en")
DEFAULT_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
DEFAULT_WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
DEFAULT_WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")


def transcript_blob_path(audio_blob_name: str) -> str:
    """Map an audio blob to the NEW Whisper-only transcript path."""
    relative_path = audio_blob_name.removeprefix(f"{AUDIO_PREFIX}/")
    transcript_name = os.path.splitext(relative_path)[0] + ".json"
    return f"{TRANSCRIPT_PREFIX}/{transcript_name}"


# def legacy_transcript_blob_path(audio_blob_name: str) -> str:
#     """OLD helper — maps to podcasts/transcripts/. Kept for reference only."""
#     relative_path = audio_blob_name.removeprefix(f"{AUDIO_PREFIX}/")
#     transcript_name = os.path.splitext(relative_path)[0] + ".json"
#     return f"{LEGACY_TRANSCRIPT_PREFIX}/{transcript_name}"


def normalize_language_code(language_code: str) -> str:
    # Whisper expects "en"; allow existing "en-US" env values.
    return language_code.split("-", 1)[0].lower()


def combine_segments(segments: list[Any]) -> str:
    return " ".join(
        segment.text.strip()
        for segment in segments
        if getattr(segment, "text", "").strip()
    )


def load_whisper_model(
    model_size: str = DEFAULT_WHISPER_MODEL,
    device: str = DEFAULT_WHISPER_DEVICE,
    compute_type: str = DEFAULT_WHISPER_COMPUTE_TYPE,
):
    from faster_whisper import WhisperModel

    log.info(
        "Loading local Whisper model=%s device=%s compute_type=%s",
        model_size,
        device,
        compute_type,
    )
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def transcribe_audio_blob(
    model: Any,
    bucket: Any,
    bucket_name: str,
    audio_blob_name: str,
    language_code: str,
) -> dict[str, Any]:
    audio_blob = bucket.blob(audio_blob_name)
    language = normalize_language_code(language_code)

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
        # GCS download only (storage egress / ops). No Speech-to-Text API.
        audio_blob.download_to_filename(tmp.name)
        segments_iter, info = model.transcribe(
            tmp.name,
            language=language,
            vad_filter=True,
        )
        segments = list(segments_iter)

    transcript = combine_segments(segments)
    return {
        "audio_gcs_uri": f"gs://{bucket_name}/{audio_blob_name}",
        "language_code": getattr(info, "language", language) or language,
        "engine": "faster-whisper",
        "model": DEFAULT_WHISPER_MODEL,
        "transcript": transcript,
        "results": [
            {
                "transcript": segment.text.strip(),
                "start": getattr(segment, "start", None),
                "end": getattr(segment, "end", None),
                "confidence": None,
            }
            for segment in segments
            if getattr(segment, "text", "").strip()
        ],
        "transcribed_at": datetime.now(timezone.utc).isoformat(),
        "transcript_prefix": TRANSCRIPT_PREFIX,
    }


def transcribe_missing(limit: int | None = None) -> dict[str, int]:
    config = load_config()
    bucket_name = config["bucket_name"]
    if not bucket_name:
        raise ValueError("Set GCP_BUCKET_NAME in .env")

    if TRANSCRIPT_PREFIX == LEGACY_TRANSCRIPT_PREFIX:
        raise ValueError(
            f"Refusing to write to legacy prefix {LEGACY_TRANSCRIPT_PREFIX!r}. "
            "Set TRANSCRIPT_PREFIX to podcasts/transcripts_whisper."
        )

    storage_client = get_storage_client(config)
    bucket = storage_client.bucket(bucket_name)
    language_code = os.getenv("TRANSCRIPTION_LANGUAGE_CODE", DEFAULT_LANGUAGE_CODE)
    model = load_whisper_model()

    stats = {"checked": 0, "transcribed": 0, "skipped": 0, "errors": 0}

    log.info(
        "Whisper-only backfill → gs://%s/%s/ "
        "(legacy %s/ is never read or written)",
        bucket_name,
        TRANSCRIPT_PREFIX,
        LEGACY_TRANSCRIPT_PREFIX,
    )

    for audio_blob in bucket.list_blobs(prefix=f"{AUDIO_PREFIX}/"):
        if not audio_blob.name.endswith(".mp3"):
            continue

        stats["checked"] += 1
        transcript_path = transcript_blob_path(audio_blob.name)
        # Safety: never allow a path under the legacy prefix.
        if transcript_path.startswith(f"{LEGACY_TRANSCRIPT_PREFIX}/"):
            raise RuntimeError(
                f"Refusing to write legacy path: {transcript_path}"
            )
        transcript_blob = bucket.blob(transcript_path)

        # Skip only if already present in the NEW Whisper prefix.
        if transcript_blob.exists():
            stats["skipped"] += 1
            continue

        attempted = stats["transcribed"] + stats["errors"]
        if limit is not None and attempted >= limit:
            break

        log.info("Transcribing locally with Whisper: %s", audio_blob.name)
        try:
            transcript_record = transcribe_audio_blob(
                model=model,
                bucket=bucket,
                bucket_name=bucket_name,
                audio_blob_name=audio_blob.name,
                language_code=language_code,
            )
            transcript_blob.upload_from_string(
                json.dumps(transcript_record, indent=2),
                content_type="application/json",
            )
            log.info("Wrote %s", transcript_path)
            stats["transcribed"] += 1
        except Exception:
            log.exception("Failed to transcribe %s", audio_blob.name)
            stats["errors"] += 1

    log.info(
        "Done. checked=%(checked)d transcribed=%(transcribed)d "
        "skipped=%(skipped)d errors=%(errors)d",
        stats,
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Transcribe podcast MP3s with local Whisper into "
            f"{TRANSCRIPT_PREFIX}/ (does not touch {LEGACY_TRANSCRIPT_PREFIX}/; "
            "no Google Speech-to-Text)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of new audio files to transcribe in this run",
    )
    args = parser.parse_args()

    transcribe_missing(limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
