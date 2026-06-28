# Terraform — `corn-off-the-cobb`

Records **current** infra only: the two existing GCS buckets (`cotc_raw`,
`podcasts-audio-files`). Target deploy infra (Artifact Registry, the scraper
service account + `cotc_raw` IAM, the Cloud Run Job, Cloud Scheduler) is not
here yet — it comes next.

## Use

```sh
cd terraform
terraform init
terraform plan   # expect: 2 to import, 0 to add, 0 to change, 0 to destroy
```

Auth uses your gcloud Application Default Credentials
(`gcloud auth application-default login`). The `import` blocks in `imports.tf`
adopt the live buckets, so the first plan/apply reconciles state without
creating anything; delete `imports.tf` after a successful apply.
