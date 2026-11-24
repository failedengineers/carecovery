# CareCovery deployment and production-readiness checklist

This checklist is intentionally separate from the feature list. A successful deployment is not the same as a production-certified medical records service. A responsible operator must verify every applicable item before entering real patient data.

## Infrastructure

- [ ] Use managed PostgreSQL with encryption at rest, restricted credentials/network access, automated backups and a tested restore plan.
- [ ] Set exact `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`; use HTTPS only.
- [ ] Generate a unique `DJANGO_SECRET_KEY` (50+ characters) and store it in the hosting provider's secret manager.
- [ ] Keep `DJANGO_DEBUG=0`; production settings reject SQLite, missing PostgreSQL, insecure SSL and missing HTTPS/email configuration.
- [ ] Confirm patient media/document uploads are disabled. The app has no file upload views/fields, and middleware rejects multipart or raw-binary upload requests with HTTP 415. No media bucket is provisioned or required.
- [ ] Configure real SMTP and test password setup/reset delivery, sender authentication, SPF, DKIM and DMARC. Render blocks outbound SMTP on ports 25, 465 and 587; use an SMTP provider/port the host allows (the Blueprint defaults to 2525, which must be supported by the provider).
- [ ] Monitor `/health/` and `/health/supabase/`, process restarts, database availability, error rates and suspicious authentication events.
- [ ] Document vendor support contacts, billing owners, service quotas and renewal dates.

## Render Free is demo-only

- [ ] For a demo, use Render Free web service with one Gunicorn worker and an external managed PostgreSQL URL; do not use Render Free Postgres as a permanent database because its free database lifetime is limited.
- [ ] Configure UptimeRobot Free (5-minute interval) to request `/health/supabase/`. That request enters Render and performs a real SQL query against Supabase, helping keep both active.
- [ ] Understand that Render Free has 750 instance hours per workspace per month; one always-on service uses about 720 hours in a 30-day month. Pings do not create an uptime SLA, prevent platform restarts, extend Render Postgres lifetime or guarantee Supabase Free project activity status.
- [ ] No patient media/document upload functionality is included; CSV import is a text-paste form and CSV exports are generated on demand.
- [ ] Do not store real patient records on a free-tier stack. Use paid managed database/storage with backups and support for production clinical workloads.

## Security and privacy

- [ ] Decide and implement MFA/SSO before granting clinical users access.
- [ ] Verify login throttling, password reset, password change, session expiry, deactivated user, expired subscription and suspended workspace behavior in staging. Legacy active/trial workspaces without an expiry receive a one-time 14-day transition trial during the role-merge startup command.
- [ ] Test cross-tenant and doctor-to-doctor isolation for every page, export, patient data access and background job.
- [ ] Confirm role boundaries: Clinic Manager combines clinic admin and reception; Doctor sees assigned patients; Nurse is limited to non-sensitive clinical records; Billing Staff cannot access clinical notes or patient records.
- [ ] Verify that no file-upload endpoints or upload fields are available to clinic users; send a multipart test request and confirm HTTP 415.
- [ ] Encrypt database backups and logs where supported. Restrict access to secrets and backup copies.
- [ ] Define retention/deletion rules, audit retention, incident response, access reviews and patient request procedures.
- [ ] Complete applicable privacy, healthcare and data-protection reviews for the places where the clinic operates.

## Data and recovery

- [ ] Back up PostgreSQL before launch.
- [ ] Run a restore drill and verify that patient, appointment, clinical and billing records restore correctly.
- [ ] Set recovery point/time objectives and verify provider backup retention meets them.
- [ ] Monitor database growth, provider egress and quotas.
- [ ] Do not put patient names, diagnoses, prescriptions, phone numbers or document contents in logs or monitoring payloads.

## Application release checks

- [ ] Generate and review migrations; commit the generated initial `crm` migration to source control for a conventional release workflow.
- [ ] Apply migrations once per release with `python manage.py migrate --noinput`.
- [ ] Run `python manage.py check --deploy` with the actual production environment.
- [ ] Run `python manage.py test crm` in CI against PostgreSQL.
- [ ] Verify appointment concurrency, payment balance calculations, inventory movement and import/export behavior against the deployed database.
- [ ] Verify static assets, account setup emails, password reset emails and `/health/` after deployment.
- [ ] Never run `python manage.py seed_demo` against production. The command refuses to run when `DJANGO_DEBUG=0`.

## Known implementation boundaries

- MFA/SSO, scheduled SMS/WhatsApp appointment reminders, patient portal, telemedicine, lab/pharmacy integrations and payment gateway are not bundled as working integrations in this release.
- Workspace expiry is checked on login and authenticated requests, platform workspace views and the `/health/` endpoint. The `expire_workspaces` management command can also be run by a scheduler.
- This release has not been executed against a live PostgreSQL provider, SMTP service or Render account from the build environment. Run staging integration tests before production use.
- Regulatory compliance and clinical safety cannot be certified by a code ZIP. Obtain review for your operational location and clinical scope.
