FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ingest.py transcribe.py run_cloud_pipeline.sh ./
RUN chmod +x run_cloud_pipeline.sh

CMD ["./run_cloud_pipeline.sh"]
