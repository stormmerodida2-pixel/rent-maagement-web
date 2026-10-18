"""Kenya's Monthly Rental Income (MRI) tax: 7.5% of the gross residential rent received each month, filed and paid
on iTax by the 20th of the next month. It applies to residential rent of KES 288,000 to 15 million a year; shops and
other commercial lets are taxed differently, so they're shown apart. This is a helper, not tax advice."""

import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Sum

from payments.models import Payment
from rentals.models import RentCharge, Unit

from .utils import _add_months

RATE = Decimal('0.075')
YEARLY_FROM = Decimal('288000')
YEARLY_TO = Decimal('15000000')
DUE_DAY = 20
REMIND_DAY = 15


def due_date(month):
    return _add_months(month, 1).replace(day=DUE_DAY)


def month_rent(owner, month):
    """Rent received in the month starting `month`: residential (taxed under MRI), commercial (shops), and the
    deposits taken off the residential total (deposits aren't rent)."""
    end = _add_months(month, 1)
    payments = Payment.objects.filter(lease__unit__property__owner=owner, paid_at__date__gte=month,
                                      paid_at__date__lt=end)
    shop = Unit.Type.SHOP
    residential = payments.exclude(lease__unit__unit_type=shop).aggregate(t=Sum('amount'))['t'] or Decimal('0')
    commercial = payments.filter(lease__unit__unit_type=shop).aggregate(t=Sum('amount'))['t'] or Decimal('0')
    # Deposits from tenants who moved in this month were part of what they paid, but they aren't rent.
    deposits = RentCharge.objects.filter(
        lease__unit__property__owner=owner, kind=RentCharge.Kind.DEPOSIT, amount__gt=0,
        lease__start_date__gte=month, lease__start_date__lt=end).exclude(lease__unit__unit_type=shop) \
        .aggregate(t=Sum('amount'))['t'] or Decimal('0')
    deposits = min(deposits, residential)
    return {'residential': residential - deposits, 'commercial': commercial, 'deposits': deposits}


def tax_on(amount):
    return (amount * RATE).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def tax_year(owner, year, today=None):
    """Each month of `year` up to now: rent, tax, due date, whether it's filed, and the year's totals."""
    from .models import TaxFiling

    today = today or datetime.date.today()
    filings = {f.month: f for f in TaxFiling.objects.filter(owner=owner, month__year=year)}
    rows = []
    for m in range(1, 13):
        month = datetime.date(year, m, 1)
        if month > today:
            break
        rent = month_rent(owner, month)
        tax = tax_on(rent['residential'])
        filing = filings.get(month)
        due = due_date(month)
        finished = _add_months(month, 1) <= today
        if filing:
            status = 'filed'
        elif not tax:
            status = 'nothing'
        elif not finished:
            status = 'running'  # the month isn't over yet
        elif today > due:
            status = 'late'
        else:
            status = 'due'
        rows.append({'month': month, **rent, 'tax': tax, 'due': due, 'filing': filing, 'status': status})
    total_rent = sum((r['residential'] for r in rows), Decimal('0'))
    months = len(rows) or 1
    projected = total_rent * 12 / months if year == today.year else total_rent
    return {
        'year': year, 'rows': rows, 'total_rent': total_rent,
        'total_tax': sum((r['tax'] for r in rows), Decimal('0')),
        'total_filed': sum((r['filing'].amount for r in rows if r['filing']), Decimal('0')),
        'commercial': sum((r['commercial'] for r in rows), Decimal('0')),
        'below_threshold': projected < YEARLY_FROM, 'above_threshold': projected > YEARLY_TO,
    }


def due_now(owner, today=None):
    """Last month's MRI, if it's unfiled and the 20th is near (from the 15th) or past: the row, else None."""
    from .models import TaxFiling

    today = today or datetime.date.today()
    month = _add_months(today.replace(day=1), -1)
    if today.day < REMIND_DAY or TaxFiling.objects.filter(owner=owner, month=month).exists():
        return None
    tax = tax_on(month_rent(owner, month)['residential'])
    return {'month': month, 'tax': tax, 'due': due_date(month), 'late': today > due_date(month)} if tax else None


def send_tax_reminders(today=None):
    """On the 15th: email/SMS landlords whose last month's MRI isn't marked filed. Returns how many."""
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.mail import send_mail
    from django.urls import reverse

    from accounts.utils import is_verified
    from notifications.models import LandlordPreferences
    from notifications.utils import send_sms, summary_phone

    today = today or datetime.date.today()
    if today.day != REMIND_DAY:
        return 0
    sent = 0
    for owner in get_user_model().objects.filter(is_active=True, properties__isnull=False).distinct():
        prefs = LandlordPreferences.for_user(owner)
        row = due_now(owner, today) if prefs.tax_reminders else None
        if row is None or tax_year(owner, row['month'].year, today)['below_threshold']:
            continue
        url = settings.SITE_URL.rstrip('/') + reverse('reports:tax') + f'?year={row["month"].year}'
        line = (f'{row["month"]:%B} rental income tax (MRI): KES {row["tax"]:,.0f}, due on iTax by '
                f'{row["due"]:%d %B}.')
        if owner.email and is_verified(owner):
            send_mail(f'KRA rental income tax due {row["due"]:%d %B}',
                      f'Hi {owner.first_name or "there"},\n\n{line}\n\nThe workings, and a place to mark it filed:\n'
                      f'{url}\n\nThis is a reminder, not tax advice. {settings.APP_NAME}',
                      None, [owner.email], fail_silently=True)
            sent += 1
        phone = summary_phone(owner, prefs)
        if phone:
            send_sms(phone, f'{settings.APP_NAME}: {line} Mark it filed: {url}')
    return sent
