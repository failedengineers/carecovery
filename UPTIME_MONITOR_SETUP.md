# CareCovery: keep Render and Supabase active

Use one external HTTP monitor to send a request to the CareCovery health endpoint every five minutes.

## Configure UptimeRobot

1. Sign in to UptimeRobot.
2. Choose **Add New Monitor**.
3. Select **HTTP(s)**.
4. Set the friendly name to `CareCovery - Render + Supabase`.
5. Set the URL to `https://YOUR-SERVICE.onrender.com/health/supabase/`, replacing `YOUR-SERVICE` with your actual Render hostname.
6. Choose the **5-minute** interval and save.
7. Confirm the monitor reports HTTP `200`.

The endpoint performs a real read query against the `crm_organization` PostgreSQL table configured by `DATABASE_URL`. That request reaches the Render web service and creates database activity on Supabase. The endpoint returns only a generic status; it does not return patient data.

## Limits you must understand

- A five-minute monitor helps prevent Render Free web-service idle spin-down after 15 minutes without inbound traffic.
- A five-minute real database query helps keep Supabase Free active; Supabase may still pause free projects, and this is not a guarantee.
- Render Free web services share 750 instance hours per workspace per month, can restart at any time, and have ephemeral filesystems.
- Do not use Render Free Postgres for long-term storage; its free database expires after 30 days.
- This is a demo/hobby keep-alive configuration, not a production availability or backup strategy. Do not store real patient information on the free-tier stack.
