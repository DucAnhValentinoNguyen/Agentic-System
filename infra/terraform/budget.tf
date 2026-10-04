variable "billing_account" { default = "01FB77-235A34-6FB07C" }

resource "google_billing_budget" "twin" {
  billing_account = var.billing_account
  display_name    = "Twin (AgentSystems) monthly budget"
  budget_filter {
    projects = ["projects/${var.project}"]
  }
  amount {
    specified_amount {
      currency_code = "EUR"
      units         = "25"
    }
  }
  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.9 }
  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
    disable_default_iam_recipients   = false
  }
}
