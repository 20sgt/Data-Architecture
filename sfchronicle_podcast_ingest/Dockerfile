FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Cheapest Cloud Run Whisper defaults (no Google Speech-to-Text).
    WHISPER_MODEL=tiny \
    WHISPER_DEVICE=cpu \
    WHISPER_COMPUTE_TYPE=int8 \
    TRANSCRIPT_PREFIX=podcasts/transcripts_whisper \
    WHISPER_MAX_EPISODES=5 \
    WHISPER_MAX_RUNTIME_MINUTES=20 \
    WHISPER_BUDGET_USD=0.25 \
    WHISPER_HOURLY_RATE_USD=0.10

# ffmpeg is required by faster-whisper for MP3 decode.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

# Prefetch tiny model into the image so weekly jobs skip Hugging Face download.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

COPY ingest.py transcribe.py enrich.py silver.py run_cloud_pipeline.sh ./
COPY data/representatives.json ./data/representatives.json
RUN chmod +x run_cloud_pipeline.sh

CMD ["./run_cloud_pipeline.sh"]
