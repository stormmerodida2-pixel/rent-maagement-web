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
| `/messages/` | SMS log |
| `/payments/settings/` | Connect the landlord's own M-Pesa Paybill/Till |
| `/t/<token>/` | Tenant's private self-service page (mobile-first) |
| `/admin/` | Superuser only |

Every page is restricted to the signed-in landlord's own records. Another landlord's record returns 404.

Styles live in `static/css/app.css`, which uses colour tokens and supports dark mode. Icons and helpers are in `rentals/templatetags/ui.py`. Set `APP_NAME` in `.env` to rebrand.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in the M-Pesa values
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

## Tests

```bash
python manage.py test
```
