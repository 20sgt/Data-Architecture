#!/usr/bin/env bash
# Deploy Cloud Run Job + Sunday 03:00 PT scheduler.
# Usage: ./deploy_cloud.sh [--execute-now]
set -euo pipefail

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
  echo "gcloud CLI not found." >&2
  exit 1
fi

echo "Using gcloud: ${GCLOUD}"
echo "Project: ${PROJECT_ID}  Region: ${REGION}  Job: ${JOB_NAME}"

"${GCLOUD}" config set project "${PROJECT_ID}"

echo "Enabling APIs..."
"${GCLOUD}" services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com

echo "IAM roles for ${SERVICE_ACCOUNT}..."
"${GCLOUD}" projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/storage.objectAdmin" \
  --condition=None >/dev/null

"${GCLOUD}" projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/logging.logWriter" \
  --condition=None >/dev/null

echo "Building ${IMAGE}..."
"${GCLOUD}" builds submit --tag "${IMAGE}" .

ENV_VARS="GCP_PROJECT_ID=${PROJECT_ID},GCP_BUCKET_NAME=${BUCKET_NAME},TRANSCRIPT_PREFIX=podcasts/transcripts_whisper,WHISPER_MODEL=tiny,WHISPER_DEVICE=cpu,WHISPER_COMPUTE_TYPE=int8,WHISPER_MAX_EPISODES=5,WHISPER_MAX_RUNTIME_MINUTES=20,WHISPER_BUDGET_USD=0.25,WHISPER_HOURLY_RATE_USD=0.10"

echo "Cloud Run Job (1 CPU / 2Gi, 30m timeout)..."
if "${GCLOUD}" run jobs describe "${JOB_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  "${GCLOUD}" run jobs update "${JOB_NAME}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --service-account "${SERVICE_ACCOUNT}" \
    --set-env-vars "${ENV_VARS}" \
    --memory 2Gi \
    --cpu 1 \
    --task-timeout 30m \
    --max-retries 0
else
  "${GCLOUD}" run jobs create "${JOB_NAME}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --service-account "${SERVICE_ACCOUNT}" \
    --set-env-vars "${ENV_VARS}" \
    --memory 2Gi \
    --cpu 1 \
    --task-timeout 30m \
    --max-retries 0
fi

echo "Scheduler permissions..."
PROJECT_NUMBER="$("${GCLOUD}" projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
SCHEDULER_SA="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"

"${GCLOUD}" iam service-accounts add-iam-policy-binding "${SERVICE_ACCOUNT}" \
  --member="${SCHEDULER_SA}" \
  --role="roles/iam.serviceAccountTokenCreator" >/dev/null

"${GCLOUD}" run jobs add-iam-policy-binding "${JOB_NAME}" \
  --region "${REGION}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/run.invoker" >/dev/null

"${GCLOUD}" iam service-accounts add-iam-policy-binding "${SERVICE_ACCOUNT}" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser" >/dev/null || true

JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

echo "Scheduler ${SCHEDULER_NAME} (${SCHEDULE} ${TIME_ZONE})..."
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
echo "Deploy complete. Schedule: ${SCHEDULE} (${TIME_ZONE})"
echo "Manual: ${GCLOUD} run jobs execute ${JOB_NAME} --region ${REGION}"

if [[ "${EXECUTE_NOW}" -eq 1 ]]; then
  echo "Executing now..."
  "${GCLOUD}" run jobs execute "${JOB_NAME}" --region "${REGION}"
fi
