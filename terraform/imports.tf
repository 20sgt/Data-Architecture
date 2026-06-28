# Terraform 1.5+ import blocks: adopt the already-created buckets so `terraform plan`
# shows no changes (we are RECORDING existing infra, not creating it). Safe to delete
# this file after the first successful `terraform apply` writes them into state.
import {
  to = google_storage_bucket.cotc_raw
  id = "corn-off-the-cobb/cotc_raw"
}

import {
  to = google_storage_bucket.podcasts_audio_files
  id = "corn-off-the-cobb/podcasts-audio-files"
}
