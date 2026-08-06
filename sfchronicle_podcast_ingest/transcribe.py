#!/usr/bin/env python3
"""Transcribe GCS podcast MP3s with faster-whisper into podcasts/transcripts_whisper/."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from ingest import AUDIO_PREFIX, get_storage_client, load_config

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TRANSCRIPT_PREFIX = os.getenv("TRANSCRIPT_PREFIX", "podcasts/transcripts_whisper")
LEGACY_TRANSCRIPT_PREFIX = "podcasts/transcripts"

DEFAULT_LANGUAGE_CODE = os.getenv("TRANSCRIPTION_LANGUAGE_CODE", "en")
DEFAULT_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
DEFAULT_WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
DEFAULT_WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

# Cloud Run spend guards (~$0.10/hr for 1 vCPU + 2 GiB). 0 / unset = unlimited locally.
DEFAULT_MAX_RUNTIME_MINUTES = float(os.getenv("WHISPER_MAX_RUNTIME_MINUTES", "0") or 0)
DEFAULT_BUDGET_USD = float(os.getenv("WHISPER_BUDGET_USD", "0") or 0)
DEFAULT_HOURLY_RATE_USD = float(os.getenv("WHISPER_HOURLY_RATE_USD", "0.10") or 0.10)


def transcript_blob_path(audio_blob_name: str) -> str:
    relative = audio_blob_name.removeprefix(f"{AUDIO_PREFIX}/")
    name = os.path.splitext(relative)[0] + ".json"
    return f"{TRANSCRIPT_PREFIX}/{name}"


def normalize_language_code(language_code: str) -> str:
    return language_code.split("-", 1)[0].lower()


def combine_segments(segments: list[Any]) -> str:
    return " ".join(
        s.text.strip() for s in segments if getattr(s, "text", "").strip()
    )


def load_whisper_model(
    model_size: str = DEFAULT_WHISPER_MODEL,
    device: str = DEFAULT_WHISPER_DEVICE,
    compute_type: str = DEFAULT_WHISPER_COMPUTE_TYPE,
):
    from faster_whisper import WhisperModel

    log.info("Loading Whisper model=%s device=%s compute_type=%s", model_size, device, compute_type)
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
        audio_blob.download_to_filename(tmp.name)
        segments_iter, info = model.transcribe(tmp.name, language=language, vad_filter=True)
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
                "transcript": s.text.strip(),
                "start": getattr(s, "start", None),
                "end": getattr(s, "end", None),
                "confidence": None,
            }
            for s in segments
            if getattr(s, "text", "").strip()
        ],
        "transcribed_at": datetime.now(timezone.utc).isoformat(),
        "transcript_prefix": TRANSCRIPT_PREFIX,
    }


def resolve_episode_limit(cli_limit: int | None) -> int | None:
    if cli_limit is not None:
        return cli_limit
    raw = os.getenv("WHISPER_MAX_EPISODES", "").strip()
    if not raw:
        return None
    return max(0, int(raw))


def estimated_cost_usd(elapsed_seconds: float, hourly_rate: float) -> float:
    return (elapsed_seconds / 3600.0) * hourly_rate


def transcribe_missing(limit: int | None = None) -> dict[str, int | float | str | None]:
    config = load_config()
    bucket_name = config["bucket_name"]
    if not bucket_name:
        raise ValueError("Set GCP_BUCKET_NAME in .env")

    if TRANSCRIPT_PREFIX == LEGACY_TRANSCRIPT_PREFIX:
        raise ValueError(
            f"Refusing to write to legacy prefix {LEGACY_TRANSCRIPT_PREFIX!r}. "
            "Set TRANSCRIPT_PREFIX to podcasts/transcripts_whisper."
        )

    episode_limit = resolve_episode_limit(limit)
    max_runtime_minutes = float(
        os.getenv("WHISPER_MAX_RUNTIME_MINUTES", str(DEFAULT_MAX_RUNTIME_MINUTES)) or 0
    )
    budget_usd = float(os.getenv("WHISPER_BUDGET_USD", str(DEFAULT_BUDGET_USD)) or 0)
    hourly_rate = float(
        os.getenv("WHISPER_HOURLY_RATE_USD", str(DEFAULT_HOURLY_RATE_USD)) or 0.10
    )

    storage_client = get_storage_client(config)
    bucket = storage_client.bucket(bucket_name)
    language_code = os.getenv("TRANSCRIPTION_LANGUAGE_CODE", DEFAULT_LANGUAGE_CODE)

    stats: dict[str, int | float | str | None] = {
        "checked": 0,
        "transcribed": 0,
        "skipped": 0,
        "errors": 0,
        "stopped_reason": None,
        "elapsed_seconds": 0.0,
        "estimated_cost_usd": 0.0,
    }

    log.info("Whisper → gs://%s/%s/ (never touches %s/)", bucket_name, TRANSCRIPT_PREFIX, LEGACY_TRANSCRIPT_PREFIX)
    log.info(
        "Guards: max_episodes=%s max_runtime_min=%s budget_usd=%s",
        episode_limit if episode_limit is not None else "unlimited",
        max_runtime_minutes or "unlimited",
        budget_usd or "unlimited",
    )

    model = None
    started = time.monotonic()

    for audio_blob in bucket.list_blobs(prefix=f"{AUDIO_PREFIX}/"):
        if not audio_blob.name.endswith(".mp3"):
            continue

        stats["checked"] = int(stats["checked"]) + 1
        transcript_path = transcript_blob_path(audio_blob.name)
        if transcript_path.startswith(f"{LEGACY_TRANSCRIPT_PREFIX}/"):
            raise RuntimeError(f"Refusing to write legacy path: {transcript_path}")

        transcript_blob = bucket.blob(transcript_path)
        if transcript_blob.exists():
            stats["skipped"] = int(stats["skipped"]) + 1
            continue

        elapsed = time.monotonic() - started
        cost = estimated_cost_usd(elapsed, hourly_rate)
        stats["elapsed_seconds"] = round(elapsed, 2)
        stats["estimated_cost_usd"] = round(cost, 4)

        attempted = int(stats["transcribed"]) + int(stats["errors"])
        if episode_limit is not None and attempted >= episode_limit:
            stats["stopped_reason"] = "max_episodes"
            log.warning("Stopping: episode budget (%s)", episode_limit)
            break
        if max_runtime_minutes > 0 and elapsed >= max_runtime_minutes * 60:
            stats["stopped_reason"] = "max_runtime"
            log.warning("Stopping: runtime budget (%.1f min)", max_runtime_minutes)
            break
        if budget_usd > 0 and cost >= budget_usd:
            stats["stopped_reason"] = "budget_usd"
            log.warning("Stopping: spend ~$%.4f >= $%.4f", cost, budget_usd)
            break

        if model is None:
            model = load_whisper_model()

        log.info("Transcribing: %s", audio_blob.name)
        try:
            record = transcribe_audio_blob(
                model=model,
                bucket=bucket,
                bucket_name=bucket_name,
                audio_blob_name=audio_blob.name,
                language_code=language_code,
            )
            transcript_blob.upload_from_string(
                json.dumps(record, indent=2),
                content_type="application/json",
            )
            log.info("Wrote %s", transcript_path)
            stats["transcribed"] = int(stats["transcribed"]) + 1
        except Exception:
            log.exception("Failed %s", audio_blob.name)
            stats["errors"] = int(stats["errors"]) + 1

    elapsed = time.monotonic() - started
    stats["elapsed_seconds"] = round(elapsed, 2)
    stats["estimated_cost_usd"] = round(estimated_cost_usd(elapsed, hourly_rate), 4)
    log.info(
        "Done checked=%s transcribed=%s skipped=%s errors=%s stopped=%s cost≈$%s",
        stats["checked"],
        stats["transcribed"],
        stats["skipped"],
        stats["errors"],
        stats["stopped_reason"],
        stats["estimated_cost_usd"],
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Whisper-transcribe missing podcast audio in GCS")
    parser.add_argument("--limit", type=int, default=None, help="Max new episodes (overrides WHISPER_MAX_EPISODES)")
    args = parser.parse_args()
    transcribe_missing(limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
