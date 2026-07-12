#!/usr/bin/env python3
"""
Create transcripts for podcast MP3 files stored in GCS.

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
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from google.cloud import speech

from ingest import AUDIO_PREFIX, get_storage_client, load_config

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

TRANSCRIPT_PREFIX = "podcasts/transcripts"
DEFAULT_LANGUAGE_CODE = os.getenv("TRANSCRIPTION_LANGUAGE_CODE", "en-US")


def transcript_blob_path(audio_blob_name: str) -> str:
    relative_path = audio_blob_name.removeprefix(f"{AUDIO_PREFIX}/")
    transcript_name = os.path.splitext(relative_path)[0] + ".json"
    return f"{TRANSCRIPT_PREFIX}/{transcript_name}"


def get_speech_client(config: dict[str, str | None]) -> speech.SpeechClient:
    key_path = config["service_account_key"]
    if key_path and os.path.isfile(key_path):
        return speech.SpeechClient.from_service_account_file(key_path)
    return speech.SpeechClient()


def combine_transcript(response: speech.LongRunningRecognizeResponse) -> str:
    return " ".join(
        result.alternatives[0].transcript.strip()
        for result in response.results
        if result.alternatives
    )


def transcribe_audio_blob(
    speech_client: speech.SpeechClient,
    bucket_name: str,
    audio_blob_name: str,
    language_code: str,
) -> dict[str, Any]:
    audio = speech.RecognitionAudio(uri=f"gs://{bucket_name}/{audio_blob_name}")
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.MP3,
        language_code=language_code,
        enable_automatic_punctuation=True,
        model="latest_long",
    )

    operation = speech_client.long_running_recognize(config=config, audio=audio)
    response = operation.result(timeout=7200)
    transcript = combine_transcript(response)

    return {
        "audio_gcs_uri": f"gs://{bucket_name}/{audio_blob_name}",
        "language_code": language_code,
        "transcript": transcript,
        "results": [
            {
                "transcript": result.alternatives[0].transcript,
                "confidence": result.alternatives[0].confidence,
            }
            for result in response.results
            if result.alternatives
        ],
        "transcribed_at": datetime.now(timezone.utc).isoformat(),
    }


def transcribe_missing(limit: int | None = None) -> dict[str, int]:
    config = load_config()
    bucket_name = config["bucket_name"]
    if not bucket_name:
        raise ValueError("Set GCP_BUCKET_NAME in .env")

    storage_client = get_storage_client(config)
    speech_client = get_speech_client(config)
    bucket = storage_client.bucket(bucket_name)
    language_code = os.getenv("TRANSCRIPTION_LANGUAGE_CODE", DEFAULT_LANGUAGE_CODE)

    stats = {"checked": 0, "transcribed": 0, "skipped": 0, "errors": 0}

    for audio_blob in bucket.list_blobs(prefix=f"{AUDIO_PREFIX}/"):
        if not audio_blob.name.endswith(".mp3"):
            continue

        stats["checked"] += 1
        transcript_path = transcript_blob_path(audio_blob.name)
        transcript_blob = bucket.blob(transcript_path)

        if transcript_blob.exists():
            stats["skipped"] += 1
            continue

        attempted = stats["transcribed"] + stats["errors"]
        if limit is not None and attempted >= limit:
            break

        log.info("Transcribing %s", audio_blob.name)
        try:
            transcript_record = transcribe_audio_blob(
                speech_client=speech_client,
                bucket_name=bucket_name,
                audio_blob_name=audio_blob.name,
                language_code=language_code,
            )
            transcript_blob.upload_from_string(
                json.dumps(transcript_record, indent=2),
                content_type="application/json",
            )
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
        description="Transcribe missing podcast audio files from GCS",
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
