# Remote Terraform state: a versioned bucket in the project (the lock is built into the GCS backend).
# Bootstrapped once with local state, then the backend block in main.tf was pointed at it.
resource "google_storage_bucket" "tfstate" {
  name                        = "${var.project}-tfstate"
  location                    = "EUROPE-WEST3"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  lifecycle_rule {
    condition {
      num_newer_versions = 30
      with_state         = "ARCHIVED"
    }
    action { type = "Delete" }
  }
  depends_on = [google_project_service.apis]
}
