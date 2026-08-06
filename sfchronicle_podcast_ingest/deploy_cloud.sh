#!/usr/bin/env bash
set -euo pipefail

# Deploy weekly podcast ingest as a Cloud Run Job + Cloud Scheduler.
# Transcription is local Whisper only (no Google Speech-to-Text charges).
# Usage:
#   ./deploy_cloud.sh
#   ./deploy_cloud.sh --execute-now

PROJECT_ID="${GCP_PROJECT_ID:-corn-off-the-cobb}"
REGION="${GCP_REGION:-us-west1}"
BUCKET_NAME="${GCP_BUCKET_NAME:-podcasts-audio-files}"
JOB_NAME="${CLOUD_RUN_JOB_NAME:-podcast-weekly-pipeline}"
SCHEDULER_NAME="${CLOUD_SCHEDULER_NAME:-podcast-weekly-trigger}"
SERVICE_ACCOUNT="${CLOUD_RUN_SERVICE_ACCOUNT:-audio-scraper@${PROJECT_ID}.iam.gserviceaccount.com}"
SCHEDULE="${CLOUD_SCHEDULE:-0 3 * * 0}"
TIME_ZONE="${CLOUD_TIME_ZONE:-America/Los_Angeles}"
IMAGE="gcr.io/${PROJECT_ID}/${JOB_NAME}"
EXECUTE_NOW=0

if [[ "${1:-}" == "--execute-now" ]]; then
  EXECUTE_NOW=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if command -v gcloud >/dev/null 2>&1; then
  GCLOUD="gcloud"
elif [[ -x "/Users/sgt/MSDS/Spring/Spring Module 2/MLOps/google-cloud-sdk/bin/gcloud" ]]; then
  GCLOUD="/Users/sgt/MSDS/Spring/Spring Module 2/MLOps/google-cloud-sdk/bin/gcloud"
else
  echo "gcloud CLI not found. Install Google Cloud SDK, then rerun." >&2
  exit 1
fi

echo "Using gcloud: ${GCLOUD}"
echo "Project: ${PROJECT_ID}"
echo "Region: ${REGION}"
echo "Job: ${JOB_NAME}"
echo "Service account: ${SERVICE_ACCOUNT}"

"${GCLOUD}" config set project "${PROJECT_ID}"

echo "Enabling required APIs..."
"${GCLOUD}" services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com

echo "Ensuring service account IAM roles..."
"${GCLOUD}" projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/storage.objectAdmin" \
  --condition=None >/dev/null

"${GCLOUD}" projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/logging.logWriter" \
  --condition=None >/dev/null

echo "Building and pushing image ${IMAGE}..."
"${GCLOUD}" builds submit --tag "${IMAGE}" .

echo "Creating/updating Cloud Run Job..."
if "${GCLOUD}" run jobs describe "${JOB_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  "${GCLOUD}" run jobs update "${JOB_NAME}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --service-account "${SERVICE_ACCOUNT}" \
    --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},GCP_BUCKET_NAME=${BUCKET_NAME},TRANSCRIPT_PREFIX=podcasts/transcripts_whisper" \
    --memory 2Gi \
    --cpu 1 \
    --task-timeout 2h \
    --max-retries 1
else
  "${GCLOUD}" run jobs create "${JOB_NAME}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --service-account "${SERVICE_ACCOUNT}" \
    --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},GCP_BUCKET_NAME=${BUCKET_NAME},TRANSCRIPT_PREFIX=podcasts/transcripts_whisper" \
    --memory 2Gi \
    --cpu 1 \
    --task-timeout 2h \
    --max-retries 1
fi

echo "Granting Cloud Scheduler permission to run the job..."
PROJECT_NUMBER="$("${GCLOUD}" projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
SCHEDULER_SA="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"

"${GCLOUD}" iam service-accounts add-iam-policy-binding "${SERVICE_ACCOUNT}" \
  --member="${SCHEDULER_SA}" \
  --role="roles/iam.serviceAccountTokenCreator" >/dev/null

"${GCLOUD}" run jobs add-iam-policy-binding "${JOB_NAME}" \
  --region "${REGION}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" >/dev/null

# Cloud Build needs to act as the runtime SA when deploying some resources.
"${GCLOUD}" iam service-accounts add-iam-policy-binding "${SERVICE_ACCOUNT}" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser" >/dev/null || true

JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

echo "Creating/updating Cloud Scheduler job (${SCHEDULE} ${TIME_ZONE})..."
if "${GCLOUD}" scheduler jobs describe "${SCHEDULER_NAME}" --location "${REGION}" >/dev/null 2>&1; then
  "${GCLOUD}" scheduler jobs update http "${SCHEDULER_NAME}" \
    --location "${REGION}" \
    --schedule "${SCHEDULE}" \
    --time-zone "${TIME_ZONE}" \
    --uri "${JOB_URI}" \
    --http-method POST \
    --oauth-service-account-email "${SERVICE_ACCOUNT}"
else
  "${GCLOUD}" scheduler jobs create http "${SCHEDULER_NAME}" \
    --location "${REGION}" \
    --schedule "${SCHEDULE}" \
    --time-zone "${TIME_ZONE}" \
    --uri "${JOB_URI}" \
    --http-method POST \
    --oauth-service-account-email "${SERVICE_ACCOUNT}"
fi

echo
echo "Deploy complete."
echo "Weekly schedule: ${SCHEDULE} (${TIME_ZONE})"
echo "Manual run: ${GCLOUD} run jobs execute ${JOB_NAME} --region ${REGION}"

if [[ "${EXECUTE_NOW}" -eq 1 ]]; then
  echo "Executing job now..."
  "${GCLOUD}" run jobs execute "${JOB_NAME}" --region "${REGION}"
fi
