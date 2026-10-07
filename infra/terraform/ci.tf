# Terraform from CI, with two identities that cannot do each other's job.
#   twin-tf-plan  read-only: may read the state bucket, nothing else. Runs on every push to main (plan without refresh).
#   twin-tf-apply broad: manages the project. Only a job that runs in the GitHub environment "production", which needs
#                 a human approval, can become this account (the token's subject names the environment).
resource "google_service_account" "tf_plan" {
  account_id   = "twin-tf-plan"
  display_name = "Terraform plan (CI, read-only)"
}

resource "google_service_account" "tf_apply" {
  account_id   = "twin-tf-apply"
  display_name = "Terraform apply (CI, behind an approval)"
}

resource "google_storage_bucket_iam_member" "plan_reads_state" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.tf_plan.email}"
}

resource "google_storage_bucket_iam_member" "apply_writes_state" {
  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin" # includes the lock object
  member = "serviceAccount:${google_service_account.tf_apply.email}"
}

resource "google_project_iam_member" "tf_apply" {
  for_each = toset([
    "roles/editor", "roles/resourcemanager.projectIamAdmin", "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser", "roles/iam.workloadIdentityPoolAdmin", "roles/secretmanager.admin",
    "roles/serviceusage.serviceUsageAdmin", "roles/run.admin",
  ])
  project = var.project
  role    = each.key
  member  = "serviceAccount:${google_service_account.tf_apply.email}"
}

resource "google_service_account_iam_member" "plan_from_main" {
  service_account_id = google_service_account.tf_plan.name
  role               = "roles/iam.workloadIdentityUser"
  # The provider already admits only this repository, so the branch alone is enough to identify the job.
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.ref/refs/heads/main"
}

resource "google_service_account_iam_member" "apply_from_production" {
  service_account_id = google_service_account.tf_apply.name
  role               = "roles/iam.workloadIdentityUser"
  # Only a job that runs in the GitHub environment "production" (which needs a reviewer's approval) carries this claim.
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.environment/production"
}

output "tf_plan_sa" { value = google_service_account.tf_plan.email }
output "tf_apply_sa" { value = google_service_account.tf_apply.email }
