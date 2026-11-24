# CareCovery — Doctor & Clinic CRM

CareCovery is a Django CRM for **individual doctors** and **multi-doctor clinics**. Each account is an isolated workspace. Within a clinic, each doctor has a distinct patient panel and clinical history, while the Clinic Manager handles both clinic administration and front-desk/reception operations from one account type.

This project includes production-oriented application controls and deployment configuration. It is **not a guarantee of production certification**. Do not store real patient information until the hosting, database backups, access controls, monitoring, clinical workflow and applicable privacy requirements have been verified for your organization.

## Included modules

- Patient registry, MRN, demographics, contact and emergency details, allergies, chronic conditions, tags, patient timeline and CSV import.
- Doctor-scoped patient access, doctor profiles and directory.
- Appointment scheduling, appointment status workflow, overlap validation, filters and daily waiting queue.
- Medical records, symptoms, examination, diagnoses, treatment plans, notes, vitals, prescriptions and follow-up dates.
- No patient media/file uploads; the application rejects multipart and raw binary upload requests with HTTP 415. Patient information is stored as structured records in PostgreSQL. CSV patient imports use pasted CSV text, and exports are generated on demand.
- Follow-up tasks, invoices, discounts, tax, partial payments and balance tracking.
- Expense ledger, inventory list, low-stock status, stock movement history and adjustments.
- Reports, patient import/export, role-scoped CSV exports, notifications and activity/audit history.
- Platform workspace onboarding, trial/license durations, manual suspension and automatic expiry suspension.
- WhatsApp and call support shortcuts for **+91 97173 24135**.

## Account types and roles

### Workspace types

- **Individual Doctor:** one doctor account with an independent practice workspace and patient panel.
- **Clinic:** one tenant with multiple doctors and clinic staff. Each doctor has their own assigned patient panel and clinical records.

### Platform roles

- **Super Admin:** all platform workspace metadata, create trial or paid-license accounts, choose duration, renew or suspend access, and inspect platform operations.
- **Onboarding Agent:** create trial workspaces for a selected number of days and see workspaces they onboarded. Cannot grant paid licenses or manually activate/suspend workspaces.

### Workspace roles

- **Clinic Manager:** combined clinic administrator and receptionist/front-desk role. Manages patients, appointments, queue, team accounts, invoices, expenses, inventory, reports and activity log. This replaces the confusing separate Clinic Admin and Receptionist roles.
- **Doctor:** sees their own assigned/primary patients, appointments, clinical records, prescriptions, follow-ups and their own invoices.
- **Nurse:** supports patient and appointment workflows, can view non-sensitive clinical records, and cannot access billing ledgers.
- **Billing Staff:** handles invoices, payments and expense workflows. Clinical notes and sensitive clinical snapshots are restricted.

## Trial and license lifecycle

- Access type is **Free trial** or **Paid license**.
- Duration is selected in days (1–3650) when a workspace is created. Onboarding Agents can create trials only. Super Admins can choose either type.
- Super Admin can grant/renew a trial or paid license later from Workspace → Subscription & access. A grant starts immediately and sets a new expiry date from the current time.
- Expired trials/licenses are automatically suspended when a user next attempts access, during platform workspace listings, when the readiness endpoint is checked, and through `python manage.py expire_workspaces`. Older workspaces from before subscriptions were introduced receive a one-time 14-day transition trial by `merge_legacy_roles` if they had no expiry date.
- Suspended workspaces cannot use clinic features. A Super Admin restores access by granting a new trial/license; manual suspension is also available.

## Local demo on Windows

### Easiest method

1. Extract the ZIP.
2. Open the `carecovery` folder.
3. Double-click `run_local.bat`.
4. Open `http://127.0.0.1:8000/`.

The script creates a virtual environment, installs dependencies, creates the first custom-user migration, applies migrations, loads fictional demo data and starts Django. An internet connection is needed for package installation. If Windows blocks `pip.exe`, run pip through the virtual environment's interpreter, e.g. `python -m pip install -r requirements.txt`.

### PowerShell alternative

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
$env:DJANGO_DEBUG = "1"
$env:DJANGO_SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(64))"
$env:DJANGO_ALLOWED_HOSTS = "127.0.0.1,localhost"
python -m pip install -r requirements.txt
python manage.py makemigrations crm
python manage.py migrate
python manage.py merge_legacy_roles
python manage.py seed_demo
python manage.py check
python manage.py runserver 127.0.0.1:8000
```

Run automated tests using `python manage.py test crm`.

## Local demo accounts

These fictional accounts are created only by `python manage.py seed_demo` with `DJANGO_DEBUG=1`. Never run this command against production data.

| Role / workspace | Username | Password |
|---|---|---|
| Super Admin | `superadmin` | `CareCovery@2026` |
| Onboarding Agent | `onboarding` | `Onboard@2026` |
| Clinic Manager — Northstar Family Clinic | `clinicmanager` | `ClinicManager@2026` |
| Doctor — Cardiology | `doctor1` | `DoctorOne@2026` |
| Doctor — Dermatology | `doctor2` | `DoctorTwo@2026` |
| Nurse | `nurse` | `NurseUser@2026` |
| Billing Staff | `billing` | `BillingUser@2026` |
| Individual Doctor — Dr. Neha Kapoor | `individualdoctor` | `NehaDoctor@2026` |

The old `reception` demo login is disabled. Existing database accounts with legacy `clinic_admin` or `receptionist` roles are converted to `clinic_manager` by `python manage.py merge_legacy_roles`.

## Deploy to Render Free with Supabase for a demo

The repository includes `render.yaml`. It configures one Render Free web service with one Gunicorn worker and a Supabase PostgreSQL database supplied through `DATABASE_URL`. Patient media/document uploads are disabled at the view and middleware layers, so no S3 bucket or media storage is required. The `/health/` endpoint checks the app database and processes due workspace expirations; `/health/supabase/` is the external keep-alive endpoint. The endpoint reads the actual `crm_organization` table, not just the application process.

### What you must provide

Render Free does **not** provide a permanent PostgreSQL database or persistent web-service disk. The blueprint therefore expects:

1. **Managed PostgreSQL URL:** for a free demo, use an external managed PostgreSQL service such as Supabase Free and paste its PostgreSQL connection string into `DATABASE_URL`. Require SSL. Do not use Render's Free Postgres for a permanent database: it expires after its 30-day free lifetime.
2. **Email provider:** configure a real authenticated SMTP account in the `EMAIL_*` and `DEFAULT_FROM_EMAIL` variables so account setup and password-reset emails can be delivered. Render blocks outbound SMTP ports 25, 465 and 587; use a supported port such as 2525 only if your email provider offers it.
3. **Render hostname:** if you rename the Render service or attach a custom domain, update `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` to the exact host/origin.

### Render deployment steps

1. Push this folder to a private GitHub repository. Do not commit `.env` files, database URLs, SMTP credentials or real patient information.
2. In Render, choose **New → Blueprint**, connect the repository and let Render read `render.yaml`.
3. Fill the prompted environment values: Supabase `DATABASE_URL` and SMTP host/user/password/from address. No S3 or media storage credentials are needed.
4. Before the first normal release, run the local helper once so it creates `crm/migrations/0001_initial.py`, then commit that migration to the repository. Commit every future generated migration alongside its model change. Render's build command can create a first migration as a bootstrap fallback, but committed migrations are required for reliable future schema changes.
5. Deploy. Render collects static files, applies committed migrations and starts the single-worker Gunicorn process.
6. Confirm `https://YOUR-SERVICE.onrender.com/health/` returns an `ok` status before trying the login page.
7. Create the first real Super Admin with `python manage.py createsuperuser` from a controlled one-off shell/console. Do not use the public demo users on the hosted database.

### Keep Render and Supabase active with one monitor

See the one-page [Uptime monitor setup guide](UPTIME_MONITOR_SETUP.md) for the exact UptimeRobot fields.


A single external monitor can touch both services. Use **UptimeRobot Free** (5-minute monitoring interval):

1. Sign in to UptimeRobot and choose **Add New Monitor**.
2. Select **HTTP(s)** as the monitor type.
3. Name it `CareCovery - Render + Supabase`.
4. Enter `https://YOUR-SERVICE.onrender.com/health/supabase/`, replacing the hostname with the actual Render service URL.
5. Select a **5-minute** monitoring interval and save.
6. Confirm the monitor receives HTTP `200`.

Keep `/health/supabase/` public; it returns only a generic health status and does not expose patient information.

This route executes a real SQL read against the `crm_organization` table on the PostgreSQL database configured in `DATABASE_URL`, then checks and suspends expired workspaces. Because the browser request reaches Render and Django queries Supabase, one monitor provides inbound traffic to the Render web service and database activity on Supabase. Do not put patient data in the monitor name, URL or alert payload.

A five-minute ping is comfortably inside Render's 15-minute idle timeout, and Supabase says a few real database requests per day are usually enough to avoid inactivity pausing. Neither provider guarantees that monitoring will prevent every pause or restart. The Render Free web service still has 750 instance hours per workspace per month and can restart; Supabase Free may still pause if activity is judged too low. This is a demo/hobby configuration, not a production availability guarantee.

### Important free-plan limits

- Render Free web instances have an ephemeral filesystem, can spin down when idle and have 750 instance hours per workspace each calendar month. One service kept awake for 30 days uses about 720 hours; a 31-day month uses about 744. A five-minute monitor can use nearly all of the 750-hour monthly allowance.
- Render Free PostgreSQL expires after 30 days. Use Supabase for this configuration, not Render Free Postgres.
- Supabase Free projects may pause after about one week of low activity. The `/health/supabase/` monitor performs a real database query; Supabase says a few real requests per day usually suffice, but this is not a guarantee.
- Free database plans have limited storage/compute and backup/restore options. Verify provider retention and take independent encrypted backups.
- This stack is for demos and evaluation, not production patient records. For real clinics, use paid infrastructure, managed backups, monitoring, security testing and a tested recovery plan.

## Security baseline and environment

The project supports Django 5.2, PostgreSQL, Gunicorn, WhiteNoise, `django-axes` and Argon2. User media/document uploads are not supported. Production settings require a long random `DJANGO_SECRET_KEY`, exact allowed hosts, HTTPS trusted origins, a PostgreSQL `DATABASE_URL`, database SSL and real SMTP credentials.

Start from `.env.production.example`. Keep `DJANGO_DEBUG=0` in production. For local demo use `DJANGO_DEBUG=1`; the local-only secret is not suitable for deployment.

Useful endpoints:

- `/` — role-specific dashboard
- `/accounts/login/` — sign in
- `/accounts/password_reset/` — password reset
- `/health/` — database readiness and expiry processing
- `/health/supabase/` — external Render + Supabase keep-alive monitor target
- `/django-admin/` — restricted Django platform administration

## Recommended production verification

Before real data is entered, complete every item in `PRODUCTION_READINESS.md`: independent tenant/access tests, MFA decision, encrypted database backups, restore drill, privacy/consent and retention processes, email delivery tests, load and penetration testing, clinical workflow sign-off and incident response.
