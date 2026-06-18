#!/usr/bin/env python3
"""
Download SF Chronicle podcast episodes from Megaphone RSS feeds into GCS.

Usage:
  python ingest.py                 # backfill + exit
  python ingest.py --watch         # poll for new episodes (default: every 6 hours)
  python ingest.py --watch --interval 3600
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv
from google.cloud import storage
from google.oauth2 import service_account

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

SHOW_FEEDS: dict[str, str] = {
    "fifth-and-mission": "https://feeds.megaphone.fm/fifth-and-mission",
    "extra-spicy": "https://feeds.megaphone.fm/extraspicy",
    "giants-splash-as-plus": "https://feeds.megaphone.fm/doubleplay",
    "datebook": "https://feeds.megaphone.fm/datebook",
    "the-doodler": "https://feeds.megaphone.fm/thedoodler",
    "warriors-off-court": "https://feeds.megaphone.fm/warriorsoffcourt",
    "fixing-our-city": "https://feeds.megaphone.fm/fixingourcity",
    "chronicled-kamala-harris": "https://feeds.megaphone.fm/chronicled",
}

MANIFEST_PATH = "podcasts/_manifest.json"
METADATA_PREFIX = "podcasts/metadata"
AUDIO_PREFIX = "podcasts/audio"
DEFAULT_POLL_SECONDS = 6 * 60 * 60


@dataclass
class EpisodeRecord:
    episode_id: str
    show_slug: str
    title: str
    description: str
    pub_date: str | None
    duration_sec: str | None
    guid: str
    source_url: str
    gcs_uri: str
    ingested_at: str


def load_config() -> dict[str, str | None]:
    return {
        "project_id": os.getenv("GCP_PROJECT_ID"),
        "bucket_name": os.getenv("GCP_BUCKET_NAME"),
        "service_account_key": os.getenv("GCP_SERVICE_ACCOUNT_KEY"),
    }


def episode_id(guid: str) -> str:
    return hashlib.sha256(guid.encode("utf-8")).hexdigest()[:16]


def get_storage_client(config: dict[str, str | None]) -> storage.Client:
    key_path = config["service_account_key"]
    project_id = config["project_id"]
    if key_path and os.path.isfile(key_path):
        credentials = service_account.Credentials.from_service_account_file(key_path)
        return storage.Client(project=project_id, credentials=credentials)
    return storage.Client(project=project_id)


def load_manifest(bucket: storage.Bucket) -> dict[str, Any]:
    blob = bucket.blob(MANIFEST_PATH)
    if not blob.exists():
        return {"episodes": {}}
    return json.loads(blob.download_as_text())


def save_manifest(bucket: storage.Bucket, manifest: dict[str, Any]) -> None:
    blob = bucket.blob(MANIFEST_PATH)
    blob.upload_from_string(
        json.dumps(manifest, indent=2),
        content_type="application/json",
    )


def audio_blob_path(show_slug: str, ep_id: str) -> str:
    return f"{AUDIO_PREFIX}/{show_slug}/{ep_id}.mp3"


def metadata_blob_path(show_slug: str, ep_id: str) -> str:
    return f"{METADATA_PREFIX}/{show_slug}/{ep_id}.json"


def parse_duration(entry: dict[str, Any]) -> str | None:
    duration = entry.get("itunes_duration")
    if duration is not None:
        return str(duration)
    return None


def get_audio_url(entry: dict[str, Any]) -> str | None:
    for enclosure in entry.get("enclosures", []):
        if enclosure.get("type", "").startswith("audio/"):
            return enclosure.get("href") or enclosure.get("url")
    links = entry.get("links", [])
    for link in links:
        if link.get("type", "").startswith("audio/"):
            return link.get("href")
    return None


def download_episode(
    bucket: storage.Bucket,
    show_slug: str,
    entry: dict[str, Any],
) -> EpisodeRecord | None:
    audio_url = get_audio_url(entry)
    if not audio_url:
        log.warning("No audio URL for %s / %s", show_slug, entry.get("title"))
        return None

    guid = entry.get("id") or entry.get("guid") or entry.get("link")
    if not guid:
        log.warning("No guid for %s / %s", show_slug, entry.get("title"))
        return None

    ep_id = episode_id(guid)
    audio_path = audio_blob_path(show_slug, ep_id)
    audio_blob = bucket.blob(audio_path)

    if not audio_blob.exists():
        log.info("Downloading %s: %s", show_slug, entry.get("title"))
        response = requests.get(audio_url, stream=True, timeout=300)
        response.raise_for_status()
        audio_buffer = io.BytesIO()
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                audio_buffer.write(chunk)
        audio_buffer.seek(0)
        audio_blob.upload_from_file(
            audio_buffer,
            content_type="audio/mpeg",
            rewind=True,
        )
    else:
        log.debug("Already in GCS: %s", audio_path)

    record = EpisodeRecord(
        episode_id=ep_id,
        show_slug=show_slug,
        title=entry.get("title", ""),
        description=entry.get("summary", "") or entry.get("description", ""),
        pub_date=entry.get("published") or entry.get("updated"),
        duration_sec=parse_duration(entry),
        guid=guid,
        source_url=audio_url,
        gcs_uri=f"gs://{bucket.name}/{audio_path}",
        ingested_at=datetime.now(timezone.utc).isoformat(),
    )

    meta_blob = bucket.blob(metadata_blob_path(show_slug, ep_id))
    meta_blob.upload_from_string(
        json.dumps(asdict(record), indent=2),
        content_type="application/json",
    )
    return record


def ingest_all(config: dict[str, str | None]) -> dict[str, int]:
    bucket_name = config["bucket_name"]
    if not bucket_name:
        raise ValueError("Set GCP_BUCKET_NAME in .env")

    client = get_storage_client(config)
    bucket = client.bucket(bucket_name)
    manifest = load_manifest(bucket)

    stats = {"shows": 0, "checked": 0, "new": 0, "skipped": 0, "errors": 0}

    for show_slug, feed_url in SHOW_FEEDS.items():
        stats["shows"] += 1
        log.info("Fetching feed: %s", show_slug)
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            log.exception("Failed to parse feed %s", feed_url)
            stats["errors"] += 1
            continue

        if feed.bozo and not feed.entries:
            log.error("Feed error for %s: %s", show_slug, feed.bozo_exception)
            stats["errors"] += 1
            continue

        for entry in feed.entries:
            stats["checked"] += 1
            guid = entry.get("id") or entry.get("guid") or entry.get("link")
            if guid and guid in manifest["episodes"]:
                stats["skipped"] += 1
                continue

            try:
                record = download_episode(bucket, show_slug, entry)
            except Exception:
                log.exception("Failed episode %s / %s", show_slug, entry.get("title"))
                stats["errors"] += 1
                continue

            if record:
                manifest["episodes"][record.guid] = asdict(record)
                save_manifest(bucket, manifest)
                stats["new"] += 1

    log.info(
        "Done. shows=%(shows)d checked=%(checked)d new=%(new)d "
        "skipped=%(skipped)d errors=%(errors)d",
        stats,
    )
    return stats


def watch(config: dict[str, str | None], interval_seconds: int) -> None:
    log.info("Watching for new episodes every %d seconds", interval_seconds)
    while True:
        try:
            ingest_all(config)
        except Exception:
            log.exception("Ingest run failed")
        time.sleep(interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest SF Chronicle podcasts from Megaphone RSS into GCS",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll RSS feeds on an interval for new episodes",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL_SECONDS", DEFAULT_POLL_SECONDS)),
        help=f"Seconds between polls in watch mode (default: {DEFAULT_POLL_SECONDS})",
    )
    args = parser.parse_args()

    config = load_config()
    if not config["bucket_name"]:
        log.error("Missing GCP_BUCKET_NAME. Copy .env.example to .env and fill in values.")
        return 1

    if args.watch:
        watch(config, args.interval)
        return 0

    ingest_all(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
