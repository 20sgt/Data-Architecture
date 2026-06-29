terraform {
  required_version = ">= 1.5.0" # import blocks
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}


variable "project_id" {
  type    = string
  default = "corn-off-the-cobb"
}


provider "google" {
  project = var.project_id

  # Don't stamp a goog-terraform-provisioned label on adopted buckets (keeps the import clean).
  add_terraform_attribution_label = false
}


# --- GCS BUCKETS ---

# Raw landing zone — holds the scraper's ingest_date= partitions (the YTD scrape).
resource "google_storage_bucket" "cotc_raw" {
  name     = "cotc_raw"
  location = "US-WEST1"

  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # cotc_raw is a hierarchical-namespace (folder-enabled) bucket. This block is REQUIRED —
  # omitting it makes the provider plan a destroy-and-recreate, which would wipe the scrape.
  hierarchical_namespace {
    enabled = true
  }

  # Soft delete is explicitly OFF on this bucket (retention 0, not the 7-day default).
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  # ponytail: force_destroy stays false — this holds the scrape; don't let `terraform destroy`
  # wipe it without an explicit opt-in.
  force_destroy = false

  lifecycle {
    # GCS returns an empty encryption{} on import; ignore it so plan stays clean (no CMEK here).
    ignore_changes = [encryption]
  }
}


resource "google_storage_bucket" "podcasts_audio_files" {
  name     = "podcasts-audio-files"
  location = "US-EAST1"

  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  soft_delete_policy {
    retention_duration_seconds = 604800 # 7 days (GCS default)
  }

  force_destroy = false

  lifecycle {
    ignore_changes = [encryption] # see cotc_raw — empty encryption{} import artifact
  }
}
