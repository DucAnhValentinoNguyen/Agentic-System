# Continuous deployment: GitHub Actions deploys without any stored key.
# GitHub proves who it is with a short-lived OIDC token (workload identity federation); GCP maps that token, only for
# this repository's main branch, to a small "deployer" service account.
variable "github_repo" { default = "DucAnhValentinoNguyen/Agentic-System" }

resource "google_project_service" "cd_apis" {
  for_each           = toset(["iamcredentials.googleapis.com", "sts.googleapis.com"])
  service            = each.key
  disable_on_destroy = false
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.cd_apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  # Only this repository, only its main branch, can ever become the deployer.
  attribute_condition = "assertion.repository == '${var.github_repo}' && assertion.ref == 'refs/heads/main'"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account" "deployer" {
  account_id   = "twin-deployer"
  display_name = "Twin CD (GitHub Actions)"
}

# Least privilege: push images, update Cloud Run services, act as the two runtime accounts. Nothing else.
resource "google_project_iam_member" "deployer_roles" {
  for_each = toset(["roles/artifactregistry.writer", "roles/run.developer"])
  project  = var.project
  role     = each.key
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_acts_as" {
  for_each = {
    agent     = google_service_account.agent.name
    retrieval = google_service_account.retrieval.name
  }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "github_impersonates_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

output "wif_provider" { value = google_iam_workload_identity_pool_provider.github.name }
output "deployer_sa" { value = google_service_account.deployer.email }
