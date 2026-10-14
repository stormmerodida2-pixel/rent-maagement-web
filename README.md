# Rental Manager

Property/rental manager for landlords: track tenants and rent, collect payments over M-Pesa (STK Push and Paybill), send SMS reminders and receipts, and log maintenance requests. Tenants get a private self-service page.

Built with Django. Landlords sign up themselves and do everything in the app. The Django admin is only for the platform superuser.

| URL | What |
| --- | --- |
| `/` | Public home page |
| `/signup/`, `/login/`, `/password-reset/` | Landlord sign-up (with email confirmation), login and password reset |
| `/welcome/` | Getting-started checklist: property → tenants → lease → M-Pesa |
| `/dashboard/` | Collection rate, arrears with one-click reminders, recent payments, open repairs |
| `/properties/` | Properties and their units. A property can be created with a run of units (A1…A6). |
| `/tenants/` | Tenants, with search |
| `/leases/` | Leases (active / owing / ended). Start, edit or end a lease. Each lease page has a statement. |
| `/payments/` | All payments, record cash/bank payments, assign unmatched M-Pesa payments |
| `/maintenance/` | Repairs by status and priority |
| `/reports/` | Monthly billed vs collected per property, 12-month trend, CSV exports |
| `/complaints/` | Complaints and messages from tenants (anything that isn't a repair) |
| `/contact/` | Contact us, for anyone: visitors, landlords (as "Help & support" in the app) |
| `/activity/` | Activity log: who changed what, with before/after values |
| `/messages/` | SMS log |
| `/payments/settings/` | Connect the landlord's own M-Pesa Paybill/Till |
| `/t/<token>/` | Tenant's private self-service page (mobile-first) |
| `/admin/` | Superuser only |

Every page is restricted to the signed-in landlord's own records. Another landlord's record returns 404.

Styles live in `static/css/app.css`, which uses colour tokens. Every page has a **Light / Dark / System** switch (`static/js/theme.js`). The choice is saved in the browser under the same key Django's admin uses, so the app and admin always match. "System" follows the device's setting. Printed statements and receipts stay white like paper. Icons and helpers are in `rentals/templatetags/ui.py`. Set `APP_NAME` in `.env` to rebrand.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # local development settings (DJANGO_DEBUG=1)
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Landlords create their own accounts at `/signup/`. You can also create one from the command line with `python manage.py create_landlord jane`.

## Day-to-day flow

1. Follow the getting-started checklist: add a property with its units, add tenants, then start a lease. Starting a lease bills the current month straight away.
2. Bill rent: `python manage.py generate_rent_charges` bills the current month's rent and service charges for every active lease and adds any late fees that are due. It is safe to run repeatedly, so put it in a daily cron job. New leases are billed for their first month when they start.
3. Tenants pay in one of three ways, and each M-Pesa payment is recorded automatically:
   - **Paybill**: the tenant pays the Paybill with their lease's account number (`LEASE<id>`, shown on the lease page).
   - **STK push**: you click **Send STK prompt** on a lease page, or the tenant clicks **Pay with M-Pesa** on their portal. The PIN prompt pops up on their phone.
   - **Cash/bank**: you record these under **Payments → Record cash/bank payment**, or from the lease page.
4. Remind tenants who owe rent. `python manage.py send_rent_reminders` texts every tenant with a balance and skips anyone reminded in the last 3 days. You can also use the **Send SMS reminder** button on the dashboard or lease page. Tenants get an SMS receipt for every M-Pesa payment.
5. Tenants report repairs on their portal, and you can also log them under **Maintenance requests**. Marking one *Resolved* stamps `resolved_at`.

Suggested cron (server time):

```cron
0 6 * * *  cd /path/to/app && venv/bin/python manage.py generate_rent_charges
0 9 * * *  cd /path/to/app && venv/bin/python manage.py send_rent_reminders
*/5 * * * * cd /path/to/app && venv/bin/python manage.py check_pending_stk
```

### Charges, fees and deposits

A tenant's balance is everything charged minus everything paid. Charges can be:

- **Rent**, billed monthly.
- **Service charge**: a fixed monthly amount set on the lease (garbage, security…), billed with rent.
- **Water**: enter meter readings for a whole property at once under **Properties → Water readings**. Each reading bills `(new − last) × the property's water rate` to the unit's tenant. The first reading for a unit only sets the baseline.
- **Late fee**: set on the lease with a grace period. It's added once a month by `generate_rent_charges` when rent is still unpaid after the grace period.
- **Deposit**: billed when the lease starts, so you can see when it has been paid.
- **One-off charges and credits** from **Add charge** on a lease, e.g. a repair the tenant caused. A negative amount is a credit.

**Move-out** (End lease) settles the deposit. It records any deductions (with a reason the tenant can see) and applies the deposit held against what's owed. It then tells you whether the tenant still owes money or is due a refund. Tick "I've already paid back any refund" to close the account at zero.

### Correcting mistakes

Money records are never deleted, so the history always adds up:

- **Void** a cash/bank payment or a charge from the lease statement, giving a reason. It stops counting towards the balance and reports, and is listed under **Voided** on the lease page. A voided monthly charge (rent, service charge, late fee) isn't billed again for that month. To bill a corrected amount, use **Add charge**.
- **A mistyped water reading** can be voided from the Water readings page; its water charge is voided with it and the previous reading becomes the baseline again. Only a unit's latest reading can be voided, because each charge is worked out from the one before it, so void back from the newest.
- **M-Pesa payments can't be voided**, because the money really arrived. **Move** them to the tenant who actually paid.
- Every change is recorded in the **Activity** log with who did it, when, and the before/after values. M-Pesa secrets are masked. The log can't be edited.

### Stuck M-Pesa prompts

If Safaricom's STK callback never arrives, `check_pending_stk` (every 5 minutes) asks Safaricom what happened to prompts pending for more than 2 minutes. There's also a **Check status** button on the lease page.

- **Paid:** the tenant is credited. Safaricom's status answer doesn't include the receipt number, so the payment shows "receipt pending" until the callback or Paybill confirmation arrives. That later confirmation fills in the receipt on the same payment instead of crediting the tenant twice.
- **Cancelled or failed:** the prompt is marked failed with Safaricom's reason.
- **Still unresolved after 24 hours, for any reason:** marked failed. That covers Safaricom never answering, changed M-Pesa keys, and a removed M-Pesa account, so nothing is retried forever.

### Complaints and support

- **Tenants → landlord:** from their private page, tenants open **Messages** and send a complaint or question (rent/billing, noise/neighbours, security, services, other). The landlord gets an email and a counter on **Complaints** in the sidebar, then replies and sets the status (Open, In progress, Resolved). The tenant gets an SMS with a link to the reply. If the tenant replies to a resolved complaint, it re-opens. Repairs still go through **Maintenance**.
- **Anyone → you:** the **Contact us** page (home page footer, and **Help & support** in the app) saves the message and emails it to `SUPPORT_EMAIL`, with the sender as reply-to. All messages are also listed in the admin under *Support messages*, where you can tick them off as handled. It has spam protection (a hidden honeypot field) and a limit of 5 messages per IP per hour.

### Statements, receipts and exports

- Lease page → **Print statement**, and **Receipt** links on every payment. These are print-ready pages: use "Print / Save as PDF" in the browser. Tenants can open their own statement and receipts from their portal.
- **Reports → Rent roll (CSV)** lists every active lease with rent, deposit held and balance. **Payments (CSV)** exports a month's payments. Both open in Excel.

### Tenant portal

Each tenant has a secret link, `/t/<token>/`, that works without a login. It is shown on the lease page, and it is included in reminder SMSes. On it the tenant can:

- see their balance and recent payments
- pay by STK push (always to their own phone number on file)
- report repairs and follow their status

If a link leaks, use the "Reset self-service link" action on the tenant.

A lease's balance is its total charges minus its total payments. A negative balance means the tenant has paid ahead.

## M-Pesa (Daraja) setup

Each landlord connects **their own** Paybill or Till, so rent goes straight to them and the platform never holds tenants' money.

### Platform (once)

1. Set `MPESA_CALLBACK_BASE_URL` to a public HTTPS URL Safaricom can reach. In development, run `ngrok http 8000` and use the ngrok URL. Add its host to `DJANGO_ALLOWED_HOSTS`.
2. Set `FIELD_ENCRYPTION_KEY`, which encrypts landlords' consumer secrets and passkeys in the database. Without it, a key is derived from `DJANGO_SECRET_KEY`, and changing that secret makes saved credentials unreadable, so landlords would have to re-enter them.

### Each landlord (in the app: Settings → M-Pesa)

1. Create an app at https://developer.safaricom.co.ke with Lipa Na M-Pesa enabled.
2. Enter the environment, Paybill or Till details, consumer key and secret, and passkey. In sandbox, use shortcode `174379` and the passkey shown on the form.
3. Click **Test**, then **Register** (Paybill URLs). After changing `MPESA_CALLBACK_BASE_URL`, re-register everyone with `python manage.py mpesa_register_c2b`.

Each M-Pesa account has its own secret token in its webhook URLs (`/payments/hooks/<token>/...`). That token identifies which landlord a callback belongs to. The paths avoid the word "mpesa" because Safaricom rejects C2B URLs that contain it.

### How payments are matched

Paybill payments are matched only against the receiving landlord's leases, in this order:

1. By account number. The `LEASE` prefix is required, but case, spaces and dashes don't matter.
2. By payer phone, if it equals exactly one of that landlord's active tenants.
3. Otherwise the payment is stored as **unmatched**. The landlord sees a warning on the dashboard and assigns it under **Payments → Unmatched**. It can't be assigned to another landlord's lease.

Till (Buy Goods) payments carry no account number, so they rely on phone matching or manual assignment. A Paybill is recommended.

If Safaricom enables external validation on a shortcode, payments with unrecognised account numbers are rejected on the tenant's phone.

All webhooks are idempotent. Safaricom retries don't create duplicates, and a receipt that arrives through both the STK callback and C2B is recorded once.

## Accounts and security

- New landlords confirm their email before they can connect M-Pesa. The confirmation link is valid for 3 days and can be resent from the banner in the app.
- Password reset is at `/password-reset/`. Reset emails don't reveal whether an address has an account.
- Rate limits:
  - sign-ups: 5 per IP per hour
  - logins: 5 failed attempts per username per 15 minutes, and 30 attempts per IP
  - password-reset requests: 5 per IP per hour
- Rate limits are stored in Django's cache, so use a shared cache (Redis/Memcached) when running more than one process. Behind a reverse proxy, make sure `REMOTE_ADDR` is the real client IP.
- Set `EMAIL_HOST` and the other email settings in `.env` to send real email (any SMTP provider). Without them, emails are printed to the console.

## SMS (Africa's Talking)

By default `SMS_BACKEND=console`: messages are only logged and stored, and appear as "Logged only" in the SMS log. To send for real:

1. Create an account at https://africastalking.com.
2. Set `SMS_BACKEND=africastalking`, `AT_USERNAME` (use `sandbox` for the simulator) and `AT_API_KEY`. `AT_SENDER_ID` is optional.
3. Set `SITE_URL` to the public URL so portal links in the SMS work.

Reminders name the landlord's own Paybill/account or Till. If a landlord hasn't connected M-Pesa, reminders only include the portal link.

Each SMS is logged, including any failures, on the lease page and under **SMS log**. Set `SMS_PAYMENT_RECEIPTS=0` to turn off payment receipts.

## Deploying

The app ships as a Docker Compose stack:

| Service | What it does |
| --- | --- |
| **caddy** | HTTPS front door; gets and renews certificates automatically |
| **web** | the app on gunicorn (runs migrations on start); static files are served by WhiteNoise |
| **scheduler** | runs the jobs below. Run exactly one. |
| **db** | PostgreSQL 17 |
| **redis** | shared cache (login limits, M-Pesa tokens) |

Scheduled jobs (no cron needed; times are Nairobi time, and a job missed during downtime runs on restart):

| Job | When |
| --- | --- |
| `check_pending_stk` | every 5 minutes |
| `backup_db` | 02:00, keeps `BACKUP_KEEP_DAYS` days |
| `clearsessions` | 03:00 |
| `generate_rent_charges` | 06:00 (rent, service charges, late fees) |
| `send_rent_reminders` | 09:00 |

### First deploy (any Linux VPS, 2 GB RAM is plenty to start)

1. Install Docker. Point your domain's DNS **A record** at the server, and open ports **80 and 443**.
2. On the server:
   ```bash
   git clone git@github.com:stormmerodida2-pixel/rent-maagement-web.git rentals && cd rentals
   cp .env.production.example .env
   nano .env        # fill in every Required value; the commands to generate secrets are in the file
   docker compose up -d --build
   docker compose exec web python manage.py createsuperuser
   ```
3. Open `https://your-domain/healthz/`. It should say `"status": "ok"` with `"scheduler": "ok"`, and the site should load over HTTPS.
4. **Keep a copy of `FIELD_ENCRYPTION_KEY` somewhere safe** (a password manager). Without it, the M-Pesa credentials saved in a backup can't be decrypted.
5. M-Pesa webhooks use `SITE_URL` automatically. Each landlord clicks **Register** under Settings → M-Pesa.

### Day to day

```bash
docker compose logs -f web scheduler          # logs
git pull && docker compose up -d --build      # deploy an update (migrations run automatically)
docker compose exec web python manage.py check --deploy
```

- **Backups** go to the `backups` volume every night. **Copy them off the server**, because a backup on the same disk won't survive losing the server. For example:
  - `docker compose cp scheduler:/backups ./backups-copy`, or
  - sync to cloud storage with rclone from the host's crontab.
- **Restore** a backup with:
  `docker compose exec -T db pg_restore --clean --no-owner -U rentals -d rentals < rentals-YYYYMMDD-HHMMSS.dump`
- **Error alerts:** set `SENTRY_DSN` (free at sentry.io) to be notified of every server error. Tenants' personal data isn't sent.
- **Monitoring:** point an uptime monitor at `/healthz/`. It returns 503 if the database or cache is down.
- After a few days of confirmed HTTPS, raise `DJANGO_HSTS_SECONDS` to `31536000`.

### Live payments, monthly report cards, install on phones

- **Live "just paid" alerts:** every landlord page checks for new payments every 15 seconds (every minute in a background tab) and shows a notice that opens the lease. More than two at once collapse into "+N more". **Turn on payment alerts** on the dashboard enables system notifications while the app is in the background. The dashboard's numbers, arrears and recent payments refresh themselves. These checks are marked as passive, so they never stop an idle session from logging out.
- **Monthly report card:** on the 1st, at 07:00, landlords get last month's results by email: collected against billed, arrears, the tenants owing most, occupancy and repairs, compared with the month before. An optional short SMS goes to the phone number set under **Notifications**. It's sent once per landlord per month (a missed day catches up), skips months with no activity, and only emails confirmed addresses. **Send me last month's report now** previews it.
- **Install on phones (PWA):** landlords get an **Install app** button in the menu, and the app opens on the dashboard. Tenants can install their own rent page, which opens straight to it. iPhones show "Share → Add to Home Screen" instructions. The service worker only caches static files and an offline page; pages and data are never stored on the device. Installing needs HTTPS, which the production setup provides; `localhost` also works for testing.

### Search engines

- **`/sitemap.xml`** lists only the public pages (home, sign-up, login, contact), always with the domain from `SITE_URL`.
- **`/robots.txt`** points crawlers to the sitemap and keeps them out of private areas: the admin, dashboards and tenants' private links (`/t/…`).
- **Private by default:** every page is marked `noindex` unless it opts in. Only the four public pages are indexable.
- **Link previews:** the public pages carry canonical links and link-preview tags, so shared links (WhatsApp, Facebook, X) show the logo and a description.
- **After going live:** add your domain in [Google Search Console](https://search.google.com/search-console) and submit `https://your-domain/sitemap.xml`.

### CI/CD (GitHub Actions)

Every push and pull request runs `.github/workflows/ci-cd.yml`:

1. **Lint:** `ruff` looks for likely bugs (unused or undefined names, risky patterns). Style is not checked.
2. **Tests:** the full suite on **PostgreSQL 17 and Redis**, like production. It also fails on missing migrations and on errors in Django's deployment checklist.
3. **Production stack smoke test:** builds the Docker image and starts the whole Compose stack. It then checks `/healthz/` through Caddy over HTTPS, confirms pages render and HTTP redirects to HTTPS, and takes a real `pg_dump` backup.
4. **Deploy:** only on `main`, only after 1–3 pass, and only once you've switched it on. It connects to the server over SSH, checks out exactly the tested commit, and rebuilds with `docker compose up -d --build --wait`. It then fails loudly if the live `/healthz/` doesn't come back healthy.

Dependabot opens weekly pull requests for dependency updates, and CI tests each one.

**Switching deploys on** (once the server is set up as above):

1. On the server, create a deploy key for GitHub Actions and allow it to log in:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C github-actions
   cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys
   ```
   The server also needs to be able to `git fetch`, so add a read-only **deploy key** for the server to the repo (repo Settings → Deploy keys).
2. In the GitHub repo, go to **Settings → Secrets and variables → Actions**.
   - **Secrets:**
     - `DEPLOY_SSH_KEY`: the contents of `~/.ssh/github_deploy` (the private key)
     - `DEPLOY_KNOWN_HOSTS`: the output of `ssh-keyscan your-server` (run it from your own computer, and check the fingerprint)
   - **Variables:**
     - `DEPLOY_HOST`: the server's address
     - `DEPLOY_USER`: the SSH user (must be allowed to run `docker`)
     - `DEPLOY_PATH`: e.g. `/home/deploy/rentals`
     - `SITE_URL`: e.g. `https://rent.example.com`
     - `DEPLOY_ENABLED`: `true`
3. Optional: under **Settings → Environments → production**, add yourself as a required reviewer, so each deploy waits for your click.

To deploy without pushing, use **Actions → CI/CD → Run workflow**. To roll back, revert the bad commit and push.

Run the same checks locally:
```bash
pip install -r requirements-dev.txt
ruff check . && python manage.py makemigrations --check --dry-run && python manage.py test
```

### Security defaults

- With `DJANGO_DEBUG` off, the app refuses to start without `DJANGO_SECRET_KEY` and `FIELD_ENCRYPTION_KEY`.
- HTTP redirects to HTTPS. Cookies are secure-only, HSTS is on, and the security headers are set.
- Only the domain in `SITE_URL` is accepted as a host.
- Login limits cover the Django admin's login page too, and share one counter with the app's login, so switching pages gives no extra guesses.
- **M-Pesa callbacks are checked against Safaricom's published IP addresses** (`MPESA_CALLBACK_IPS`) for production accounts. Safaricom doesn't sign callbacks, so this stops someone who learns a webhook URL from reporting fake payments:
  - **A Paybill notification from elsewhere is held, not credited.** It appears under Unmatched payments marked "Unverified, check statement", and is recorded in the Activity log. If the genuine notification for the same receipt then arrives from Safaricom, it replaces the held copy and is credited normally.
  - **An STK result from elsewhere is ignored.** The 5-minute status check asks Safaricom directly instead.
  - Sandbox accounts aren't checked.
- Behind Caddy, `TRUSTED_PROXY_COUNT=1` makes the login and sign-up limits see each visitor's real IP. Without it, every visitor would look like the proxy and share one limit.

To try the stack on your own computer, add yourself to the docker group first (`sudo usermod -aG docker $USER`, then log out and in).

## Tests

```bash
python manage.py test
```
