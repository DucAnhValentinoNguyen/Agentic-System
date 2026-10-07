terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
  backend "gcs" {
    bucket = "agentsystems-510414-tfstate"
    prefix = "twin"
  }
}

variable "project" { default = "agentsystems-510414" }
variable "region" { default = "europe-west3" }
variable "daily_budget_usd" { default = 2 }
variable "monthly_budget_usd" { default = 12 }
variable "daily_turns_per_ip" { default = 150 }
variable "session_store" { default = "firestore" } # "memory" keeps a chat in one process (then max_instances must be 1)
variable "max_instances" { default = 3 }           # spend is capped by the shared budgets, not by this number
variable "image_tag" { description = "Image tag to deploy (git sha)" }
variable "alert_email" { description = "Where alerts and budget emails go (set ALERT_EMAIL in .env)" }
variable "fault_inject" { default = "" }
variable "rate_per_minute" { default = 12 } # per IP; raised only during load tests
variable "throttle_cooldown_s" { default = 10 }
variable "booking_page_url" { default = "https://calendar.app.google/fE5TZPUmwwr3Rpn49" } # Duc-Anh's own appointment page
variable "ab_variant" { default = "A" }                                                   # A = plain RAG; B = RAG + claim verification; auto = 50/50 split

provider "google" {
  project               = var.project
  region                = var.region
  user_project_override = true
  billing_project       = var.project
}

resource "google_artifact_registry_repository" "twin" {
  repository_id = "twin"
  location      = var.region
  format        = "DOCKER"
}

resource "google_service_account" "agent" {
  account_id   = "twin-agent-api"
  display_name = "Twin agent-api runtime"
}

resource "google_project_iam_member" "agent_roles" {
  for_each = toset(["roles/aiplatform.user", "roles/cloudtrace.agent", "roles/monitoring.metricWriter"])
  project  = var.project
  role     = each.key
  member   = "serviceAccount:${google_service_account.agent.email}"
}

locals {
  image = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.twin.repository_id}/agent-api:${var.image_tag}"
}

resource "google_cloud_run_v2_service" "agent" {
  name                = "twin-agent-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  lifecycle {
    # The deploy workflow rolls out new images with gcloud; Terraform must not roll them back.
    ignore_changes = [client, client_version, scaling, template[0].containers[0].image]
  }

  template {
    service_account  = google_service_account.agent.email
    timeout          = "3600s" # WebSocket connections live inside one request
    session_affinity = true
    scaling {
      min_instance_count = 0
      # With SESSION_STORE=memory a chat lives in one process, so this must stay 1. With "firestore" any
      # instance can serve any chat; spend is capped by the shared daily and monthly budgets, not by this number.
      max_instance_count = var.max_instances
    }
    max_instance_request_concurrency = 80
    containers {
      image = local.image
      resources {
        limits            = { cpu = "1", memory = "512Mi" }
        startup_cpu_boost = true
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project
      }
      dynamic "env" {
        for_each = google_secret_manager_secret.s
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
      env {
        name  = "RATE_PER_MINUTE"
        value = tostring(var.rate_per_minute)
      }
      env {
        name  = "THROTTLE_COOLDOWN_S"
        value = tostring(var.throttle_cooldown_s)
      }
      env {
        name  = "FIRESTORE_ENABLED"
        value = "true"
      }
      env {
        name  = "BOOKING_PAGE_URL"
        value = var.booking_page_url
      }
      env {
        name  = "AB_VARIANT"
        value = var.ab_variant
      }
      env {
        name  = "RETRIEVAL_ADDR"
        value = "${trimprefix(google_cloud_run_v2_service.retrieval.uri, "https://")}:443"
      }
      env {
        name  = "RETRIEVAL_AUDIENCE"
        value = google_cloud_run_v2_service.retrieval.uri
      }
      env {
        name  = "FAULT_INJECT"
        value = var.fault_inject
      }
      env {
        name  = "DAILY_BUDGET_USD"
        value = tostring(var.daily_budget_usd)
      }
      env {
        name  = "MONTHLY_BUDGET_USD"
        value = tostring(var.monthly_budget_usd)
      }
      env {
        name  = "DAILY_TURNS_PER_IP"
        value = tostring(var.daily_turns_per_ip)
      }
      env {
        name  = "SESSION_STORE"
        value = var.session_store
      }
      startup_probe {
        http_get { path = "/health" }
        period_seconds    = 3
        failure_threshold = 20
      }
    }
  }
  depends_on = [google_project_iam_member.agent_roles, google_secret_manager_secret_iam_member.agent, google_secret_manager_secret_version.s]
}

# Public endpoint: the browser widget calls it directly. Abuse is bounded by the origin
# check, per-IP rate limit, session cap and daily budget inside the service.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.agent.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---- gRPC retrieval service: private, same image, different entrypoint ----
resource "google_service_account" "retrieval" {
  account_id   = "twin-retrieval"
  display_name = "Twin retrieval runtime"
}

resource "google_project_iam_member" "retrieval_vertex" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.retrieval.email}"
}

resource "google_cloud_run_v2_service" "retrieval" {
  name                = "twin-retrieval"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  lifecycle {
    # The deploy workflow rolls out new images with gcloud; Terraform must not roll them back.
    ignore_changes = [client, client_version, scaling, template[0].containers[0].image]
  }

  template {
    service_account = google_service_account.retrieval.email
    scaling {
      min_instance_count = 1 # a cold start would blow the caller's 3 s deadline
      max_instance_count = 2
    }
    containers {
      image   = local.image
      command = ["sh", "-c"]
      args    = ["exec .venv/bin/python services/retrieval/server.py"]
      ports {
        name           = "h2c" # gRPC needs HTTP/2; the WebSocket service keeps HTTP/1.1
        container_port = 8080
      }
      resources {
        limits            = { cpu = "1", memory = "512Mi" }
        startup_cpu_boost = true
      }
      startup_probe {
        tcp_socket { port = 8080 }
        period_seconds    = 3
        failure_threshold = 20
      }
    }
  }
  depends_on = [google_project_iam_member.retrieval_vertex]
}

resource "google_cloud_run_v2_service_iam_member" "agent_calls_retrieval" {
  name     = google_cloud_run_v2_service.retrieval.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent.email}"
}

output "url" { value = google_cloud_run_v2_service.agent.uri }
output "retrieval_url" { value = google_cloud_run_v2_service.retrieval.uri }
