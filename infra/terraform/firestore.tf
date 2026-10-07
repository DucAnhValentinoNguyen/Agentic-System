# ---- Persistence: Firestore (Native), TTL retention, index; online-judge job + schedule ----
locals {
  apis = [
    "run.googleapis.com", "artifactregistry.googleapis.com", "aiplatform.googleapis.com",
    "secretmanager.googleapis.com", "monitoring.googleapis.com", "logging.googleapis.com",
    "billingbudgets.googleapis.com", "calendar-json.googleapis.com", "gmail.googleapis.com",
    "firestore.googleapis.com", "cloudscheduler.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.key
  disable_on_destroy = false
}

resource "google_firestore_database" "default" {
  name            = "(default)"
  location_id     = var.region
  type            = "FIRESTORE_NATIVE"
  deletion_policy = "ABANDON" # keep visitor data if this config is ever destroyed by mistake
  depends_on      = [google_project_service.apis]
}

# The privacy page promises 30-day deletion: Firestore enforces it with a TTL on expire_at.
resource "google_firestore_field" "ttl" {
  for_each   = toset(["turns", "audit", "counters", "ipdays", "sessions", "checkpoints", "blobs", "writes"])
  collection = each.key
  field      = "expire_at"
  ttl_config {}
  index_config {} # TTL field needs no index
  depends_on = [google_firestore_database.default]
}

# Online judge job: newest unjudged answerable turns first.
resource "google_firestore_index" "pending_judgments" {
  collection = "turns"
  fields {
    field_path = "judge_pending"
    order      = "ASCENDING"
  }
  fields {
    field_path = "ts"
    order      = "DESCENDING"
  }
  depends_on = [google_firestore_database.default]
}

resource "google_project_iam_member" "agent_firestore" {
  project = var.project
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.agent.email}"
}

# ---- Online scoring: Cloud Run Job + Cloud Scheduler ----
resource "google_cloud_run_v2_job" "judge" {
  name                = "twin-online-judge"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.agent.email
      timeout         = "600s"
      max_retries     = 0
      containers {
        image   = local.image
        command = ["sh", "-c"]
        args    = ["cd services/agent-api && exec /srv/.venv/bin/python -m app.online_judge"]
        resources { limits = { cpu = "1", memory = "512Mi" } }
        env {
          name  = "FIRESTORE_ENABLED"
          value = "true"
        }
        env {
          name  = "JUDGE_MAX_PER_RUN"
          value = "6"
        }
        dynamic "env" {
          for_each = { for k, v in google_secret_manager_secret.s : k => v if k != "GOOGLE_CALENDAR_TOKEN" }
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value.secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.agent, google_project_iam_member.agent_firestore]
}

resource "google_service_account" "scheduler" {
  account_id   = "twin-scheduler"
  display_name = "Triggers the online judge job"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_job" {
  name     = google_cloud_run_v2_job.judge.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "judge" {
  name      = "twin-online-judge"
  region    = var.region
  schedule  = "0 */3 * * *" # the free judge quota is 200k tokens/day; 8 runs x 6 turns stays well under it
  time_zone = "Europe/Berlin"
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project}/locations/${var.region}/jobs/${google_cloud_run_v2_job.judge.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_runs_job, google_project_service.apis]
}
