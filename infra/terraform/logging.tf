# Chat logs contain visitor messages; the privacy page promises 30-day deletion.
resource "google_logging_project_bucket_config" "default" {
  project        = var.project
  location       = "global"
  bucket_id      = "_Default"
  retention_days = 30
}
