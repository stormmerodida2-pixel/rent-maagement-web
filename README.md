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

Styles live in `static/css/app.css`, which uses colour tokens. Every page has a **Light / Dark / System** switch (`static/js/theme.js`). The choice is saved in the browser under the same key Django's admin uses, so the app and admin always match. "System" follows the device's setting. Printed statements and receipts stay white like paper. Icons and helpers are in `rentals/templatetags/ui.py`. Set `APP_NAME` in your env file to rebrand.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
touch .env_local   # settings for your computer (read by settings/local.py); empty works to start
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Landlords create their own accounts at `/signup/`. You can also create one from the command line with `python manage.py create_landlord jane`.

## Project layout

Settings live in `settings/`: `base.py` (shared), `local.py` (your computer; the default for `manage.py` and the tests), `development.py` (a test copy online) and `production.py` (the live site; the default for the server and Docker image). Choose one with `DJANGO_SETTINGS_MODULE`, e.g. `settings.development`. All packages are in `requirements.txt`.

Each settings file reads its own env file: `.env_local` (your computer), `.env_dev` (the test copy online) and `.env_prod` (the live site). They hold passwords and keys, so git ignores them. `.env_local` can start empty. On a server, `.env_prod` needs the values listed under **First deploy**; `.env_dev` needs the same, plus `DJANGO_SETTINGS_MODULE=settings.development` and `ENV_FILE=.env_dev`, with sandbox credentials and different keys from the live site.

Each app keeps the same small set of files:

| File | What goes in it |
|---|---|
| `models.py`, `views.py`, `forms.py`, `urls.py`, `admin.py` | the usual Django pieces |
| `utils.py` | the app's helper functions (e.g. `rentals/utils.py`: phone numbers, tenant language, billing, rent changes, view mixins; `notifications/utils.py`: SMS, report cards, move-out reminders) |
| `tests.py`, or a `tests/` folder when there are several test files | tests |

The only other module is `payments/mpesa.py`, the M-Pesa (Daraja) connection, which is big enough to keep on its own. Site-wide pages that belong to no app (sitemap, robots.txt, privacy/terms, the installable-app files) are in `config/views.py`.

## Day-to-day flow

1. Follow the getting-started checklist: add a property with its units, add tenants, then start a lease. Starting a lease bills the current month straight away.
2. Bill rent: `python manage.py generate_rent_charges` bills each active lease's rent and service charge for the month, 5 days before that tenant's rent day, and adds any late fees that are due. It is safe to run repeatedly, so put it in a daily cron job. New leases are billed for their first month when they start.
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
- Set `EMAIL_HOST` and the other email settings in your env file (`.env_local`, `.env_dev` or `.env_prod`) to send real email (any SMTP provider). Without them, emails are printed to the console.

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
   nano .env_prod   # the values below
   export COMPOSE_ENV_FILES=.env_prod   # so every docker compose command below uses it (add to ~/.bashrc)
   docker compose up -d --build
   docker compose exec web python manage.py createsuperuser
   ```
   `.env_prod` needs at least:
   ```bash
   DOMAIN=rent.example.com
   SITE_URL=https://rent.example.com
   DJANGO_SECRET_KEY=        # python3 -c "import secrets; print(secrets.token_urlsafe(50))"
   FIELD_ENCRYPTION_KEY=     # python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   BACKUP_ENCRYPTION_KEY=    # generated the same way; keep a copy OFF the server
   POSTGRES_PASSWORD=        # python3 -c "import secrets; print(secrets.token_urlsafe(24))"
   EMAIL_HOST=smtp.gmail.com  EMAIL_PORT=587  EMAIL_USE_TLS=1  EMAIL_HOST_USER=  EMAIL_HOST_PASSWORD=  DEFAULT_FROM_EMAIL=  SUPPORT_EMAIL=
   SMS_BACKEND=africastalking  AT_USERNAME=  AT_API_KEY=  AT_SENDER_ID=
   ```
   (one per line). Optional: `APP_NAME`, `SENTRY_DSN`, `DJANGO_HSTS_SECONDS`, `BACKUP_KEEP_DAYS`, `SESSION_IDLE_MINUTES`, `CSP_REPORT_ONLY`, `WEB_CONCURRENCY`, `MPESA_CALLBACK_IPS`, and `LEGAL_ENTITY_NAME`, `LEGAL_ADDRESS`, `PRIVACY_EMAIL` for the privacy and terms pages.
3. Open `https://your-domain/healthz/`. It should say `"status": "ok"` with `"scheduler": "ok"`, and the site should load over HTTPS.
4. **Keep a copy of `FIELD_ENCRYPTION_KEY` somewhere safe** (a password manager). Without it, the M-Pesa credentials saved in a backup can't be decrypted.
5. M-Pesa webhooks use `SITE_URL` automatically. Each landlord clicks **Register** under Settings → M-Pesa.

### Day to day

```bash
docker compose logs -f web scheduler          # logs
sudo systemctl start liskodi-autodeploy  # deploy now (it also runs by itself every 2 minutes; see "Automatic deploys")
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

- **Live "just paid" alerts:** every landlord page checks for new payments every 15 seconds (every minute in a background tab) and shows a notice that opens the lease. More than two at once collapse into "+N more". **Settings → Payment alerts → Phone and computer notifications** enables system notifications while the app is in the background. The dashboard's numbers, arrears and recent payments refresh themselves. These checks are marked as passive, so they never stop an idle session from logging out.
- **Monthly report card:** on the 1st, at 07:00, landlords get last month's results by email: collected against billed, arrears, the tenants owing most, occupancy and repairs, compared with the month before. An optional short SMS goes to the phone number set under **Notifications**. It's sent once per landlord per month (a missed day catches up), skips months with no activity, and only emails confirmed addresses. **Send me last month's report now** previews it.
- **Install on phones (PWA):** landlords get an **Install app** button in the menu, and the app opens on the dashboard. Tenants can install their own rent page, which opens straight to it. iPhones show "Share → Add to Home Screen" instructions. The service worker only caches static files and an offline page; pages and data are never stored on the device. Installing needs HTTPS, which the production setup provides; `localhost` also works for testing.

### English or Kiswahili for each tenant

- When adding or editing a tenant, choose **Language for SMS and their rent page**: English or Kiswahili. Tenants marked Kiswahili get their rent reminders, payment receipts, reply alerts, rent page, messages, receipts and statements in Kiswahili only. A receipt or statement prints in the tenant's language even when you print it yourself.
- Announcements have an optional **Kiswahili title and message**. Kiswahili readers get that version, and everyone else gets the English. If you leave it blank, the preview tells you how many Kiswahili readers will get the English.
- The landlord side stays in English.
- Translations live in `locale/sw/LC_MESSAGES/django.po`. After changing tenant-facing text, run `python manage.py makemessages -l sw --ignore='venv/*' --no-location --no-wrap`, fill in the new `msgstr` lines, then run `python manage.py compilemessages` (needs GNU gettext: `sudo apt install gettext`) and commit both files. A test fails if any string is untranslated or the `.mo` is out of date. The Kiswahili was written for this app; have a native speaker review it before launch.

### Privacy policy and terms

- `/privacy/` and `/terms/` are written for Kenya's Data Protection Act, 2019. Landlords are the data controllers for their tenants' data, and the platform is their processor. **They are a first draft: have an advocate review them before launch.**
- Set `LEGAL_ENTITY_NAME`, `LEGAL_ADDRESS` and `PRIVACY_EMAIL` so the pages show your business details.
- Sign-up requires accepting both. The date and the terms version (`LEGAL['TERMS_VERSION']` in settings) are saved on each landlord's profile. When you change the terms materially, bump the version and the "last updated" date.
- Register with the Office of the Data Protection Commissioner (odpc.go.ke) if the law requires it for your business.

### Rent day: ring, chime and reminding everyone

- **Rent-day ring:** the top of the dashboard shows how much of this month's billing has come in, with "N of M tenants paid up" (tenants with no balance at all). It glides forward as payments arrive, without reloading.
- **Payment toast and chime:** a new payment pops up as "💚 Wanjiku just paid KES 18,000" with a soft chime made in the browser. Browsers only play sound after you've clicked or typed on the page once, and the **Sound** switch under **Settings → Payment alerts** mutes it (remembered per browser).
- **Remind everyone who owes:** a button on the dashboard opens a preview of every tenant with a balance and their personal SMS. Anyone reminded in the last 24 hours starts unticked, and the SMS cost updates as you tick. Messages go out several at a time, so a long list still takes only a few seconds.

### Staff (caretakers)

- **Settings → Staff → Add staff:** choose their name, a username and a password (give it to them in person), and optionally which properties they look after. Untick **Can log in** when someone leaves; their past work stays in the records.
- Staff log in on the normal login page and land in a phone-first staff area. There they can:
  - see who hasn't paid and what each tenant owes
  - send an M-Pesa prompt to the tenant's own number, then watch it live until it's paid or declined; **Check with Safaricom** asks M-Pesa directly
  - record cash (dated within the last 7 days); the tenant gets an SMS receipt naming who recorded it
  - log repairs and update them
- Staff never see totals, reports, payment lists, exports, settings or other landlord pages. A middleware sends any staff request outside `/staff/` back to the staff area, so new pages are covered automatically.
- Everything staff do appears in **Activity** with "(staff)" after their name.

### Rent days and moving out (month-to-month tenancies)

- **Rent day = move-in day.** A tenant who moves in on the 12th pays a full month every 12th; each payment covers 12th to 11th. Statements show "Rent 12 Oct – 11 Nov 2026". Rent days of the 29th–31st fall back to the last day of shorter months. You can set a different rent day on the lease; existing leases keep theirs.
- Each tenant's rent is billed **5 days before their rent day**, by the daily 06:00 job, so nobody shows as owing before it's nearly due.
- **Rent changes:** **Change rent** on a lease, or on a property for several tenants at once (by a percentage, by an amount, or to a set rent, with a preview first). The new rent starts on each tenant's first rent day on or after the date you choose, and billing switches automatically. Tenants get an SMS in their language, and the change shows on their rent page. Percentage rises round to the nearest KES 50. You can cancel a change before it starts.
- **Move-out notice:** "Record move-out notice" on a lease. The date defaults to the end of a rent month at least a month away. No new rent month is billed after it. A reminder email goes out 3 days before, and "Finish move-out" settles the deposit.

### Vacancy listings

- **Vacancies** in the menu lists units you can advertise (vacant, or the tenant has given notice). **Advertise** creates a public page at `/to-let/…` with rent, deposit, date available, description and up to 8 photos.
- **Share on WhatsApp** and **Copy** put the link anywhere. The page has a link preview (title, rent, first photo) for WhatsApp and Facebook, and open listings appear in the sitemap.
- People apply with their name, phone, move-in date and number of occupants. You get an email for each application, with duplicates from the same phone ignored. A hidden field and a per-connection limit stop bots and floods.
- **Approve** adds the person as a tenant and opens the lease form for that unit. The listing closes by itself once a lease starts, and its page then says the place has been let.
- Photos are resized (max 1600 px) and saved as fresh JPEGs, so the GPS location and phone details in a photo's EXIF data are never published. On the server they're kept on the `media` Docker volume and served by Caddy. They are **not** in the nightly database backup.

### Two-step login

- **Settings → Two-step login** (caretakers: the **Two-step** tab). Scan the QR code with Google Authenticator, Microsoft Authenticator or Authy, then enter a code to turn it on. You get 10 **recovery codes** once; keep them away from your phone.
- After the password, logging in asks for the 6-digit code. Each code works only once, codes are accepted 30 seconds either side for clock drift, and 5 wrong codes start the login over. **Trust this device for 30 days** skips the code on that browser; turning two-step login off and on again forgets all trusted devices.
- A recovery code works in place of the app code once. Turning two-step login off needs your password and a code. You're emailed when it's turned on or off.
- The Django admin's login page sends you to the app's login, so admins get the same checks.
- Someone who lost their phone **and** their codes: `docker compose --env-file .env_prod exec web python manage.py disable_two_step <username>`.

### Email with Gmail (until you have your own domain)

Use a Gmail account's **app password**: Google account → Security → 2-Step Verification (must be on) → **App passwords** → create one named "Rentipay". Then set in `.env_prod`: `EMAIL_HOST=smtp.gmail.com`, `EMAIL_PORT=587`, `EMAIL_USE_TLS=1`, `EMAIL_HOST_USER` and `DEFAULT_FROM_EMAIL` to the Gmail address, `EMAIL_HOST_PASSWORD` to the app password. Run `docker compose --env-file .env_prod up -d`, then test with `docker compose --env-file .env_prod exec web python manage.py sendtestemail you@example.com`. Gmail allows about 500 emails a day. With your own domain, Amazon SES is the better long-term choice.

### Importing from Excel

**Properties → Import from Excel** brings in a building's units and the tenants already living there. Download the template (the second sheet explains each column), fill in one row per unit, upload it (.xlsx or .csv, up to 1,000 rows), check the preview, and confirm.

- **Unit** and **Rent** are required. A row with no tenant creates or updates a vacant unit. A tenant whose phone matches an existing contact reuses that contact.
- **Balance owed now** becomes a "Balance brought forward" line, or "Credit brought forward" for a minus amount. If this month's rent was already due on import day, it counts as part of that balance and isn't billed again. Billing continues from each tenant's next rent day.
- **Deposit** is a deposit already paid: it's recorded as held for move-out without adding to what the tenant owes.
- Carried-over amounts don't count as a month's billing in reports.
- Rows with problems (bad phone, missing rent, duplicate unit, unit already occupied) are shown in the preview and skipped. Nothing is saved until you confirm, and the import goes into Activity.

### Payment plans for arrears

When a tenant has fallen behind, open their page and choose **Set up payment plan**, e.g. KES 45,000 as KES 10,000 every Friday. Preview the payments, then agree; the tenant can be sent the plan by SMS.

- Instalments are paid **on top of normal rent**. A payment first covers rent and charges that fall due after the plan started; only the rest counts towards the plan.
- Two days before each instalment, the tenant gets a reminder (daily at 9:30, once per instalment). If they've missed one, the reminder asks for both.
- While a tenant keeps to their plan, the usual balance reminders are paused, and **Remind everyone who owes** leaves them unticked.
- **Tenants → Payment plans** (and a dashboard card) shows who's on track, who's missed payments, and progress. Plans finish by themselves once cleared; cancel one to return to normal reminders.
- Tenants see their plan's progress and next payment on their rent page.

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

Dependabot opens weekly pull requests for dependency updates, and CI tests each one.

### Automatic deploys

Every push to `main` goes live by itself once those checks pass. The **server pulls** the new version, so GitHub never logs in to it: SSH stays limited to your own address and no server keys are stored on GitHub.

`deploy/autodeploy.sh` runs on the server every 2 minutes (systemd timer `liskodi-autodeploy.timer`):

1. If `main` hasn't moved, it does nothing.
2. It asks GitHub whether the CI run for the new commit passed. If CI is still running, it checks again next time. If it failed, it skips that commit.
3. It checks out the commit and rebuilds (`docker compose … up -d --build --wait`); migrations run as the web container starts. Caddy restarts only if the Caddyfile changed.
4. If the new version doesn't come up healthy, it returns to the previous one and won't retry that commit until a newer one arrives. Database migrations that already ran aren't undone, so fix forward with a new commit.

A merge is usually live about 10 minutes after you click Merge: CI takes ~8 minutes, then up to 2 minutes until the next check, plus the build.

**On the server:**
```bash
journalctl -u liskodi-autodeploy -n 50 --no-pager   # what it did, newest last
sudo systemctl start liskodi-autodeploy            # check now instead of waiting
sudo systemctl disable --now liskodi-autodeploy.timer   # pause automatic deploys
```

**Installing it on a new server** (the app in `/home/ubuntu/app`, `.env_prod` filled in, first `docker compose up` done):
```bash
sudo cp ~/app/deploy/liskodi-autodeploy.service ~/app/deploy/liskodi-autodeploy.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now liskodi-autodeploy.timer
```
The service always runs the newest `deploy/autodeploy.sh` from `main`, so changes to the script roll out too. If your repository is private, the server needs a read-only deploy key (repo Settings → Deploy keys) to `git fetch`.

To roll back, revert the bad commit and merge: the revert deploys like any other change.

### Alerts

Three layers, so you hear about problems quickly:

1. **Phone notifications (ntfy):** the server sends push notifications to a private [ntfy](https://ntfy.sh) channel. Install the free **ntfy** app (Android/iPhone) and subscribe to the topic in `~/.config/liskodi/alerts.env` on the server (`ALERT_NTFY_URL=https://ntfy.sh/<topic>`). Anyone who knows the topic name can read it, so keep it private. You'll be told when:
   - a deploy fails (the site goes back to the previous version) or is skipped because tests failed. Successful deploys send a quiet note.
   - the site isn't healthy (database, cache or scheduler down, or HTTPS not working). `deploy/healthcheck.sh` checks every 5 minutes (`liskodi-healthcheck.timer`).
   - the HTTPS certificate has 14 days or less left, or the disk is over 85% full.

   Each problem is reported once when it starts and once when it's fixed. To install on a new server, copy `deploy/liskodi-healthcheck.*` to `/etc/systemd/system/` and `systemctl enable --now liskodi-healthcheck.timer`, as for auto-deploy.
2. **From outside:** `.github/workflows/uptime.yml` checks `/healthz/` every 15 minutes from GitHub. If the whole server is down, the run fails and **GitHub emails you**. Set `SITE_URL` in that file when you move to your own domain. GitHub pauses scheduled workflows after 60 days without commits; re-enable them under Actions if that happens.
3. **Code errors (Sentry):** set `SENTRY_DSN` in `.env_prod` (free account at sentry.io, Django project) to get an email with the exact error for any page that crashes or scheduled job that fails.

Run the same checks locally:
```bash
pip install ruff==0.17.0   # the linter (only needed to run it yourself)
ruff check . && python manage.py makemigrations --check --dry-run && python manage.py test
```

### Security defaults

- With the production (or development) settings, the app refuses to start without `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY` and `BACKUP_ENCRYPTION_KEY`.
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
