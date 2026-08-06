# Terraform IaC

State lives in a shared GCS bucket (`gs://cotc-tfstate`) via the `gcs` backend,
so everyone works against the same state with locking. State is **never** committed
(`*.tfstate` is gitignored) — the backend is the sharing mechanism, not git.

## First-time setup (once, already completed)

1. Create the state bucket out-of-band — Terraform can't store its own backend's
  state. Enable versioning so a bad apply is recoverable.
2. Push existing local state up to the bucket:
  ```sh
   terraform init -migrate-state   # answer yes to copy local state -> GCS
  ```

## Use (everyone, day to day)

```sh
gcloud auth application-default login   # once per machine
cd terraform
terraform init    # pulls shared state from GCS
terraform plan    # review changes
terraform apply   # apply them; the state lock blocks concurrent applies
```

Auth uses your gcloud Application Default Credentials.