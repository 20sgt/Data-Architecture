# Weekly Legistar scrape: Cloud Scheduler -> Cloud Run Job -> gs://cotc_raw (bronze).
# The job runs the repo image (Dockerfile/entrypoint.sh); the bucket is mounted as a
# volume at /data, so entrypoint's RAW_ROOT=/data writes ingest_date= partitions
# straight into the bucket with zero GCS code in the scrapers.

locals {
  region = "us-west1"
  image  = "${local.region}-docker.pkg.dev/${var.project_id}/cotc/legistar-scraper:latest"
}

resource "google_project_service" "scrape_apis" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudbuild.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "cotc" {
  location      = local.region
  repository_id = "cotc"
  format        = "DOCKER"
  description   = "Data-Architecture images (legistar scraper)"
  depends_on    = [google_project_service.scrape_apis]
}

# One identity for weekly path: job runtime (writes bronze) and scheduler's OAuth caller (invokes the job). 
resource "google_service_account" "legistar" {
  account_id   = "sa-legistar-scraper"
  display_name = "Legistar weekly scraper (Cloud Run Job + Scheduler)"
}

resource "google_storage_bucket_iam_member" "legistar_bronze_rw" {
  bucket = google_storage_bucket.cotc_raw.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.legistar.email}"
}

resource "google_cloud_run_v2_job" "legistar_weekly" {
  name     = "legistar-weekly"
  location = local.region

  template {
    template {
      service_account       = google_service_account.legistar.email
      timeout               = "7200s" # weekly window is ~30-45 min at 2 req/s; 2h is slack
      max_retries           = 1
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2" # required for GCS volume mounts

      containers {
        image = local.image

        resources {
          limits = {
            cpu    = "1"
            memory = "2Gi" # headroom for chromium if a run ever needs enumeration
          }
        }

        env {
          name  = "RAW_ROOT"
          value = "/data" # bucket root -> /data, so entrypoint writes gs://cotc_raw/{meetings,matters}/...
        }

        volume_mounts {
          name       = "bronze"
          mount_path = "/data"
        }
      }

      volumes {
        name = "bronze"
        gcs {
          bucket    = google_storage_bucket.cotc_raw.name
          read_only = false
        }
      }
    }
  }
  depends_on = [google_project_service.scrape_apis]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invokes" {
  name     = google_cloud_run_v2_job.legistar_weekly.name
  location = local.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.legistar.email}"
}

# Wednesday 06:00 Pacific — BoS meets Tuesdays; minutes/agendas are posted by then.
resource "google_cloud_scheduler_job" "legistar_weekly" {
  name             = "legistar-weekly"
  region           = local.region
  schedule         = "0 6 * * 3"
  time_zone        = "America/Los_Angeles"
  attempt_deadline = "180s" # deadline for the trigger call only, not the job run

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${local.region}/jobs/${google_cloud_run_v2_job.legistar_weekly.name}:run"

    oauth_token {
      service_account_email = google_service_account.legistar.email
    }
  }

  retry_config {
    retry_count = 1
  }

  depends_on = [google_project_service.scrape_apis]
}
