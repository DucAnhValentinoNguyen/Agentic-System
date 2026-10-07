variable "billing_account" { default = "01FB77-235A34-6FB07C" }
variable "project_number" { default = "1058744794873" } # the budget API returns the number, not the id

resource "google_billing_budget" "twin" {
  billing_account = var.billing_account
  display_name    = "Twin (AgentSystems) monthly budget"
  budget_filter {
    projects = ["projects/${var.project_number}"]
  }
  amount {
    specified_amount {
      currency_code = "EUR"
      units         = "25"
    }
  }
  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.9 }
  threshold_rules { threshold_percent = 1.0 }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND" # warns before the money is gone
  }
  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
    disable_default_iam_recipients   = false
  }
}
