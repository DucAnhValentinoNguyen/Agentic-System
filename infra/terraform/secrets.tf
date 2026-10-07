variable "groq_api_key" {
  type      = string
  sensitive = true
}
variable "langfuse_public_key" { type = string }
variable "langfuse_secret_key" {
  type      = string
  sensitive = true
}
variable "google_calendar_token" {
  type      = string
  sensitive = true
  default   = ""
}

locals {
  secrets = {
    GROQ_API_KEY          = var.groq_api_key
    LANGFUSE_PUBLIC_KEY   = var.langfuse_public_key
    LANGFUSE_SECRET_KEY   = var.langfuse_secret_key
    GOOGLE_CALENDAR_TOKEN = var.google_calendar_token
  }
}

# The secret names are fixed, so a CI run without the values (they are placeholders there) never plans to delete them.
resource "google_secret_manager_secret" "s" {
  for_each  = toset(keys(local.secrets))
  secret_id = lower(replace(each.key, "_", "-"))
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "s" {
  for_each    = google_secret_manager_secret.s
  secret      = each.value.id
  secret_data = local.secrets[each.key]
  lifecycle {
    # Values are set when a secret is created and rotated on purpose (gcloud secrets versions add, or
    # terraform apply -replace=...). CI runs with placeholders and must never overwrite a real value.
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_iam_member" "agent" {
  for_each  = google_secret_manager_secret.s
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent.email}"
}
