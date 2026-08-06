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
    """Purpose: verify each MP3 maps to the NEW Whisper-only transcript path."""
    assert (
        transcribe.transcript_blob_path("podcasts/audio/datebook/episode-123.mp3")
        == "podcasts/transcripts_whisper/datebook/episode-123.json"
    )


def test_transcript_prefix_is_not_legacy():
    """Purpose: ensure new runs never target the undisturbed legacy prefix."""
    assert transcribe.TRANSCRIPT_PREFIX != transcribe.LEGACY_TRANSCRIPT_PREFIX
    assert transcribe.TRANSCRIPT_PREFIX == "podcasts/transcripts_whisper"


def test_combine_segments_joins_whisper_segments():
    """Purpose: verify Whisper segment texts are combined into one transcript."""
    segments = [
        SimpleNamespace(text=" First sentence. "),
        SimpleNamespace(text="Second sentence."),
    ]
    assert transcribe.combine_segments(segments) == "First sentence. Second sentence."


def test_normalize_language_code_strips_region():
    """Purpose: verify en-US env values still work with Whisper's en language code."""
    assert transcribe.normalize_language_code("en-US") == "en"


def test_transcribe_missing_skips_existing_transcripts(monkeypatch):
    """Purpose: verify reruns only transcribe MP3s missing transcript files."""
    bucket = FakeBucket(
        [
            FakeBlob("podcasts/audio/show/needs-transcript.mp3"),
            FakeBlob("podcasts/audio/show/already-transcribed.mp3"),
            FakeBlob("podcasts/audio/show/ignore.txt"),
            # Legacy path must be ignored for skip/write decisions.
            FakeBlob("podcasts/transcripts/show/already-transcribed.json", exists=True),
            FakeBlob(
                "podcasts/transcripts_whisper/show/already-transcribed.json",
                exists=True,
            ),
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
    monkeypatch.setattr(transcribe, "load_whisper_model", lambda: object())
    monkeypatch.setattr(
        transcribe,
        "transcribe_audio_blob",
        lambda **kwargs: {
            "audio_gcs_uri": f"gs://{kwargs['bucket_name']}/{kwargs['audio_blob_name']}",
            "language_code": kwargs["language_code"],
            "engine": "faster-whisper",
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

    new_transcript = bucket.blobs[
        "podcasts/transcripts_whisper/show/needs-transcript.json"
    ]
    transcript_record = json.loads(new_transcript.uploaded_string)
    assert transcript_record["transcript"] == "Generated transcript."
    assert transcript_record["engine"] == "faster-whisper"
    assert new_transcript.content_type == "application/json"
    # Legacy object must remain untouched (no upload performed on it).
    legacy = bucket.blobs["podcasts/transcripts/show/already-transcribed.json"]
    assert legacy.uploaded_string is None
