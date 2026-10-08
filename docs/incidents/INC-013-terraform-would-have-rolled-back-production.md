# INC-013: the first `terraform plan` against real state would have rolled production back to an old image (caught before applying)

**Detection.** After moving Terraform state from a local file into a versioned bucket, the first full plan listed "update in-place"
on both Cloud Run services: `image ...:ea6b3e149296 -> ...:v27`. v27 was the image from days earlier.

**Cause.** Two owners for one fact. Terraform set the image from a variable, and the deploy workflow (added the same day) rolls out
new images with `gcloud run services update`. Terraform therefore saw every deploy as drift and would have reverted it on the next
apply. The same plan also showed gcloud's own bookkeeping fields (`client`, `client_version`, a service-level `scaling` block), two
provider artefacts (the budget's project id versus project number, the dashboard JSON that the API rewrites with defaults), and the
online judge job, whose image only Terraform ever updated, so it had been running v27 since the day it was created.

**Fix.**
- Terraform ignores the image and gcloud's fields on all Cloud Run resources (`lifecycle { ignore_changes }`); the deploy workflow owns images,
  and now also updates the judge job and the billing guard.
- The budget filter uses the project number; the dashboard document is ignored after creation (its edits go through `-replace`).
- `template.revision` can not be ignored: gcloud pins a revision name, and ignoring the field made Terraform send the stale name back,
  which Cloud Run rejected (HTTP 409). It is left alone, which shows as a harmless diff after a deploy, and the CI plan avoids it by
  not refreshing.
- The CI plan job (read-only identity, no refresh, no lock) now exits 0 on a clean state, which is what makes a "changes pending" message trustworthy.
- Secrets: the secret *names* are fixed and the values are never overwritten, so CI runs with placeholders and a missing value can never
  plan the deletion of a secret.

**What went wrong on the way.** Two failed attempts, both harmless because they failed before changing anything: a for_each over a
value only known after apply, and the 409 above. A GitHub identity binding built on the exact token subject string was rejected; binding
on attributes (the branch, the environment) worked.

**Lesson.** When a second system starts changing a resource, the first system's next plan is a rollback. Read the first plan against
real state line by line before any apply, and give each fact exactly one owner.
