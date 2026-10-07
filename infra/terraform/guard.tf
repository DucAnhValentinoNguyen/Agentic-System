# Hard stop. A budget counts the project's cost before credits and publishes it to a Pub/Sub topic every few minutes; a
# small private service reads each message and, once the cost reaches the limit, detaches billing from the project.
# Then nothing can be charged any more, whatever credential an attacker holds. The price is that Twin is down until
# billing is linked again (see README, "Hard stop"). It starts in dry-run mode: set guard_dry_run = false once verified.
variable "kill_switch_eur" { default = 200 }
variable "guard_dry_run" { default = true }
variable "guard_image_tag" { default = "1db6f788f3b0" } # only for creation; the deploy workflow keeps it current

locals {
  guard_budget_name = "Twin hard stop"
  guard_image       = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.twin.repository_id}/agent-api:${var.guard_image_tag}"
}

resource "google_pubsub_topic" "budget" {
  name       = "twin-budget"
  depends_on = [google_project_service.apis]
}

resource "google_service_account" "guard" {
  account_id   = "twin-billing-guard"
  display_name = "Twin billing guard (can unlink billing from this project, nothing else)"
}

resource "google_service_account" "guard_push" {
  account_id   = "twin-billing-push"
  display_name = "Pub/Sub push to the billing guard"
}

# Project Billing Manager on this one project: it may attach or detach its billing account, nothing more.
resource "google_project_iam_member" "guard_detach" {
  project = var.project
  role    = "roles/billing.projectManager"
  member  = "serviceAccount:${google_service_account.guard.email}"
}

resource "google_cloud_run_v2_service" "guard" {
  name                = "twin-billing-guard"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # reachable, but only the push identity may invoke it
  deletion_protection = false
  lifecycle {
    ignore_changes = [client, client_version, scaling, template[0].containers[0].image]
  }
  template {
    service_account = google_service_account.guard.email
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image   = local.guard_image
      command = ["sh", "-c"]
      args    = ["exec .venv/bin/uvicorn app.billing_guard:app --app-dir services/agent-api --host 0.0.0.0 --port $PORT"]
      resources { limits = { cpu = "1", memory = "512Mi" } }
      env {
        name  = "GCP_PROJECT"
        value = var.project
      }
      env {
        name  = "GUARD_BUDGET_NAME"
        value = local.guard_budget_name
      }
      env {
        name  = "GUARD_DRY_RUN"
        value = tostring(var.guard_dry_run)
      }
    }
  }
  depends_on = [google_project_iam_member.guard_detach]
}

resource "google_cloud_run_v2_service_iam_member" "guard_invoked_by_push" {
  name     = google_cloud_run_v2_service.guard.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.guard_push.email}"
}

# Pub/Sub signs the push request as guard_push; its service agent needs permission to create that token.
resource "google_service_account_iam_member" "pubsub_signs_push" {
  service_account_id = google_service_account.guard_push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${var.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription" "guard" {
  name                 = "twin-budget-to-guard"
  topic                = google_pubsub_topic.budget.id
  ack_deadline_seconds = 30
  expiration_policy { ttl = "" } # never expires
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }
  push_config {
    push_endpoint = google_cloud_run_v2_service.guard.uri
    oidc_token {
      service_account_email = google_service_account.guard_push.email
      audience              = google_cloud_run_v2_service.guard.uri
    }
  }
  depends_on = [google_service_account_iam_member.pubsub_signs_push, google_cloud_run_v2_service_iam_member.guard_invoked_by_push]
}

resource "google_billing_budget" "guard" {
  billing_account = var.billing_account
  display_name    = local.guard_budget_name
  budget_filter {
    projects               = ["projects/${var.project_number}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS" # real usage, whether or not credits cover it
  }
  amount {
    specified_amount {
      currency_code = "EUR"
      units         = tostring(var.kill_switch_eur)
    }
  }
  threshold_rules { threshold_percent = 1.0 }
  all_updates_rule {
    pubsub_topic   = google_pubsub_topic.budget.id
    schema_version = "1.0"
  }
}

# An email the moment the guard checks in dry-run, acts, or fails to act.
resource "google_monitoring_alert_policy" "guard_fired" {
  display_name          = "Twin: the billing guard fired or failed"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.id]
  conditions {
    display_name = "guard logged a detach, a would-detach or a failure"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"twin-billing-guard\" AND (jsonPayload.message=\"guard_billing_detached\" OR jsonPayload.message=\"guard_would_detach_billing\" OR jsonPayload.message=\"guard_detach_failed\")"
    }
  }
  alert_strategy {
    notification_rate_limit { period = "300s" }
  }
  documentation {
    content   = "The hard stop reached its limit (or could not act). Read the guard logs; billing is re-linked in the console under Billing > Account management."
    mime_type = "text/markdown"
  }
  depends_on = [google_cloud_run_v2_service.guard]
}
