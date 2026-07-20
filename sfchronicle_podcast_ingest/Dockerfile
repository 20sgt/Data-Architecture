FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

# Ingest + enrich + silver (no Whisper in cloud — STT stays local/$0).
COPY ingest.py enrich.py silver.py run_cloud_pipeline.sh ./
COPY data/representatives.json ./data/representatives.json
RUN chmod +x run_cloud_pipeline.sh

CMD ["./run_cloud_pipeline.sh"]
