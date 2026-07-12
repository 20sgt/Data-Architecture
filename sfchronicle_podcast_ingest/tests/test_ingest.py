import json
from types import SimpleNamespace

import ingest


class FakeBlob:
    def __init__(self, name, exists=False, text=None):
        self.name = name
        self._exists = exists
        self.text = text
        self.uploaded_file = None
        self.uploaded_string = None
        self.content_type = None

    def exists(self):
        return self._exists

    def download_as_text(self):
        return self.text

    def upload_from_file(self, file_obj, content_type=None, rewind=False):
        if rewind:
            file_obj.seek(0)
        self.uploaded_file = file_obj.read()
        self.content_type = content_type
        self._exists = True

    def upload_from_string(self, data, content_type=None):
        self.uploaded_string = data
        self.content_type = content_type
        self._exists = True


class FakeBucket:
    def __init__(self, name="test-bucket"):
        self.name = name
        self.blobs = {}

    def blob(self, name):
        if name not in self.blobs:
            self.blobs[name] = FakeBlob(name)
        return self.blobs[name]


class FakeStorageClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, bucket_name):
        self._bucket.name = bucket_name
        return self._bucket


class FakeResponse:
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        return iter(self._chunks)


def test_get_audio_url_prefers_audio_enclosure():
    """Purpose: verify the ingest step extracts the real MP3 URL from RSS data."""
    entry = {
        "enclosures": [
            {"type": "image/jpeg", "href": "https://example.com/art.jpg"},
            {"type": "audio/mpeg", "href": "https://example.com/episode.mp3"},
        ],
    }

    assert ingest.get_audio_url(entry) == "https://example.com/episode.mp3"


def test_download_episode_uploads_audio_and_metadata(monkeypatch):
    """Purpose: verify a new episode saves both MP3 bytes and metadata to GCS paths."""
    bucket = FakeBucket()
    entry = {
        "id": "episode-guid-1",
        "title": "Episode Title",
        "summary": "Episode summary",
        "published": "Sat, 27 Jun 2026 10:00:00 -0000",
        "itunes_duration": "120",
        "enclosures": [{"type": "audio/mpeg", "href": "https://example.com/audio.mp3"}],
    }

    monkeypatch.setattr(
        ingest.requests,
        "get",
        lambda *args, **kwargs: FakeResponse([b"audio-", b"bytes"]),
    )

    record = ingest.download_episode(bucket, "test-show", entry)

    assert record is not None
    assert record.guid == "episode-guid-1"
    assert record.gcs_uri == f"gs://test-bucket/podcasts/audio/test-show/{record.episode_id}.mp3"

    audio_blob = bucket.blobs[f"podcasts/audio/test-show/{record.episode_id}.mp3"]
    assert audio_blob.uploaded_file == b"audio-bytes"
    assert audio_blob.content_type == "audio/mpeg"

    metadata_blob = bucket.blobs[f"podcasts/metadata/test-show/{record.episode_id}.json"]
    metadata = json.loads(metadata_blob.uploaded_string)
    assert metadata["title"] == "Episode Title"
    assert metadata["source_url"] == "https://example.com/audio.mp3"


def test_ingest_all_skips_episode_already_in_manifest(monkeypatch):
    """Purpose: verify reruns do not redownload podcasts already tracked in the manifest."""
    bucket = FakeBucket()
    bucket.blobs[ingest.MANIFEST_PATH] = FakeBlob(
        ingest.MANIFEST_PATH,
        exists=True,
        text=json.dumps({"episodes": {"existing-guid": {"title": "Already Saved"}}}),
    )
    entry = {
        "id": "existing-guid",
        "title": "Already Saved",
        "enclosures": [{"type": "audio/mpeg", "href": "https://example.com/audio.mp3"}],
    }

    monkeypatch.setattr(ingest, "SHOW_FEEDS", {"test-show": "https://example.com/feed.xml"})
    monkeypatch.setattr(
        ingest,
        "get_storage_client",
        lambda config: FakeStorageClient(bucket),
    )
    monkeypatch.setattr(
        ingest.feedparser,
        "parse",
        lambda feed_url: SimpleNamespace(entries=[entry], bozo=False),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("download_episode should not run for manifest entries")

    monkeypatch.setattr(ingest, "download_episode", fail_if_called)

    stats = ingest.ingest_all(
        {"bucket_name": "test-bucket", "project_id": "test-project", "service_account_key": None}
    )

    assert stats["checked"] == 1
    assert stats["skipped"] == 1
    assert stats["new"] == 0
