import json
from types import SimpleNamespace

import transcribe


class FakeBlob:
    def __init__(self, name, exists=False):
        self.name = name
        self._exists = exists
        self.uploaded_string = None
        self.content_type = None

    def exists(self):
        return self._exists

    def upload_from_string(self, data, content_type=None):
        self.uploaded_string = data
        self.content_type = content_type
        self._exists = True


class FakeBucket:
    def __init__(self, blobs):
        self.name = "test-bucket"
        self.blobs = {blob.name: blob for blob in blobs}

    def list_blobs(self, prefix):
        return [blob for blob in self.blobs.values() if blob.name.startswith(prefix)]

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


def test_transcript_blob_path_matches_audio_path():
    """Purpose: verify each MP3 maps to one predictable transcript JSON path."""
    assert (
        transcribe.transcript_blob_path("podcasts/audio/datebook/episode-123.mp3")
        == "podcasts/transcripts/datebook/episode-123.json"
    )


def test_combine_transcript_joins_recognition_segments():
    """Purpose: verify segmented Speech-to-Text responses become one readable transcript."""
    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                alternatives=[SimpleNamespace(transcript="First sentence.", confidence=0.9)]
            ),
            SimpleNamespace(
                alternatives=[SimpleNamespace(transcript="Second sentence.", confidence=0.8)]
            ),
        ]
    )

    assert transcribe.combine_transcript(response) == "First sentence. Second sentence."


def test_transcribe_missing_skips_existing_transcripts(monkeypatch):
    """Purpose: verify reruns only transcribe MP3s missing transcript files."""
    bucket = FakeBucket(
        [
            FakeBlob("podcasts/audio/show/needs-transcript.mp3"),
            FakeBlob("podcasts/audio/show/already-transcribed.mp3"),
            FakeBlob("podcasts/audio/show/ignore.txt"),
            FakeBlob("podcasts/transcripts/show/already-transcribed.json", exists=True),
        ]
    )

    monkeypatch.setattr(
        transcribe,
        "load_config",
        lambda: {
            "bucket_name": "test-bucket",
            "project_id": "test-project",
            "service_account_key": None,
        },
    )
    monkeypatch.setattr(
        transcribe,
        "get_storage_client",
        lambda config: FakeStorageClient(bucket),
    )
    monkeypatch.setattr(transcribe, "get_speech_client", lambda config: object())
    monkeypatch.setattr(
        transcribe,
        "transcribe_audio_blob",
        lambda **kwargs: {
            "audio_gcs_uri": f"gs://{kwargs['bucket_name']}/{kwargs['audio_blob_name']}",
            "language_code": kwargs["language_code"],
            "transcript": "Generated transcript.",
            "results": [],
            "transcribed_at": "2026-06-27T00:00:00+00:00",
        },
    )

    stats = transcribe.transcribe_missing()

    assert stats["checked"] == 2
    assert stats["transcribed"] == 1
    assert stats["skipped"] == 1
    assert stats["errors"] == 0

    new_transcript = bucket.blobs["podcasts/transcripts/show/needs-transcript.json"]
    transcript_record = json.loads(new_transcript.uploaded_string)
    assert transcript_record["transcript"] == "Generated transcript."
    assert new_transcript.content_type == "application/json"
