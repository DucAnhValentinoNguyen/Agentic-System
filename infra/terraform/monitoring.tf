resource "google_monitoring_notification_channel" "email" {
  display_name = "Twin alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

locals {
  svc_filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"twin-agent-api\""
  counters = {
    twin_turns              = "jsonPayload.message=\"turn\""
    twin_turns_degraded     = "jsonPayload.message=\"turn\" AND jsonPayload.degraded=true"
    twin_turns_fallback     = "jsonPayload.message=\"turn\" AND jsonPayload.fallback=true"
    twin_turns_failed       = "jsonPayload.message=\"turn_failed\""
    twin_retrieval_fallback = "jsonPayload.message=\"retrieval_fallback\""
    twin_structured_retry   = "jsonPayload.message=\"structured_parse_failed\""
  }
}

resource "google_logging_metric" "counter" {
  for_each = local.counters
  name     = each.key
  filter   = "${local.svc_filter} AND ${each.value}"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "turn_latency" {
  name            = "twin_turn_latency_ms"
  filter          = "${local.svc_filter} AND jsonPayload.message=\"turn\""
  value_extractor = "EXTRACT(jsonPayload.latency_ms)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "ms"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 20
      growth_factor      = 1.5
      scale              = 100
    }
  }
}

locals {
  channels = [google_monitoring_notification_channel.email.id]
}

resource "google_monitoring_alert_policy" "degraded" {
  display_name          = "Twin: answered without a model (degraded mode)"
  combiner              = "OR"
  notification_channels = local.channels
  documentation { content = "Every provider failed for at least one turn. Check /v1/status breakers and provider status pages; see docs/incidents/." }
  conditions {
    display_name = "degraded turns >= 1 in 10 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/twin_turns_degraded\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  depends_on = [google_logging_metric.counter]
}

resource "google_monitoring_alert_policy" "fallback_rate" {
  display_name          = "Twin: >20% of turns needed a fallback provider"
  combiner              = "OR"
  notification_channels = local.channels
  documentation { content = "Primary provider is failing or throttling. Fallback keeps users served but costs latency; see docs/incidents/INC-001." }
  conditions {
    display_name = "fallback share over 15 min"
    condition_threshold {
      filter             = "metric.type=\"logging.googleapis.com/user/twin_turns_fallback\" AND resource.type=\"cloud_run_revision\""
      denominator_filter = "metric.type=\"logging.googleapis.com/user/twin_turns\" AND resource.type=\"cloud_run_revision\""
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.2
      duration           = "0s"
      aggregations {
        alignment_period     = "900s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "900s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  depends_on = [google_logging_metric.counter]
}

resource "google_monitoring_alert_policy" "turn_failures" {
  display_name          = "Twin: turns failing with an internal error"
  combiner              = "OR"
  notification_channels = local.channels
  conditions {
    display_name = "turn_failed >= 3 in 10 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/twin_turns_failed\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 2
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  depends_on = [google_logging_metric.counter]
}

resource "google_monitoring_alert_policy" "latency" {
  display_name          = "Twin: p95 turn latency above 8 s"
  combiner              = "OR"
  notification_channels = local.channels
  conditions {
    display_name = "p95 > 8000 ms for 10 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/twin_turn_latency_ms\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 8000
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
  depends_on = [google_logging_metric.turn_latency]
}

resource "google_monitoring_alert_policy" "cloud_run_5xx" {
  display_name          = "Twin: Cloud Run 5xx responses"
  combiner              = "OR"
  notification_channels = local.channels
  conditions {
    display_name = "5xx > 5 in 5 min"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND metric.labels.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "retrieval_fallback" {
  display_name          = "Twin: gRPC retrieval falling back to keyword search"
  combiner              = "OR"
  notification_channels = local.channels
  conditions {
    display_name = "retrieval_fallback >= 5 in 10 min"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/twin_retrieval_fallback\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 4
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  depends_on = [google_logging_metric.counter]
}

resource "google_monitoring_dashboard" "twin" {
  dashboard_json = jsonencode({
    displayName = "Twin"
    mosaicLayout = {
      columns = 12
      tiles = [
        for i, w in [
          { t = "Turns", f = "metric.type=\"logging.googleapis.com/user/twin_turns\" resource.type=\"cloud_run_revision\"", a = "ALIGN_RATE" },
          { t = "Degraded turns (no model)", f = "metric.type=\"logging.googleapis.com/user/twin_turns_degraded\" resource.type=\"cloud_run_revision\"", a = "ALIGN_RATE" },
          { t = "Turns on a fallback provider", f = "metric.type=\"logging.googleapis.com/user/twin_turns_fallback\" resource.type=\"cloud_run_revision\"", a = "ALIGN_RATE" },
          { t = "Retrieval fallbacks (gRPC down or slow)", f = "metric.type=\"logging.googleapis.com/user/twin_retrieval_fallback\" resource.type=\"cloud_run_revision\"", a = "ALIGN_RATE" },
          { t = "Structured-output parse failures", f = "metric.type=\"logging.googleapis.com/user/twin_structured_retry\" resource.type=\"cloud_run_revision\"", a = "ALIGN_RATE" },
          { t = "Turn latency p95 (ms)", f = "metric.type=\"logging.googleapis.com/user/twin_turn_latency_ms\" resource.type=\"cloud_run_revision\"", a = "ALIGN_PERCENTILE_95" },
          ] : {
          xPos = (i % 2) * 6, yPos = floor(i / 2) * 4, width = 6, height = 4
          widget = {
            title = w.t
            xyChart = {
              dataSets = [{
                plotType = "LINE"
                timeSeriesQuery = { timeSeriesFilter = {
                  filter      = w.f
                  aggregation = { alignmentPeriod = "300s", perSeriesAligner = w.a }
                } }
              }]
            }
          }
        }
      ]
    }
  })
  depends_on = [google_logging_metric.counter, google_logging_metric.turn_latency]
}
