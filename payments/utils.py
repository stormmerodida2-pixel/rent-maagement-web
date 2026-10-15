"""Helpers for payments: encrypted model fields and M-Pesa statement uploads. (The live M-Pesa connection
is in mpesa.py.)"""

import base64
import csv
import datetime
import hashlib
import io
import logging
import re
from decimal import Decimal, InvalidOperation

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


# --- Encrypted model fields --------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _fernet():
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        # Fall back to a key derived from SECRET_KEY. Set FIELD_ENCRYPTION_KEY in production
        # so rotating SECRET_KEY doesn't make stored credentials unreadable.
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


class EncryptedTextField(models.TextField):
    """Text encrypted at rest with Fernet. Not usable in lookups."""

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            logger.error('Could not decrypt %s; was the encryption key changed?', self.name)
            return ''

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return _fernet().encrypt(value.encode()).decode()


# --- M-Pesa statement uploads -------------------------------------------------------------------------
# Landlords without a Daraja app (or catching up on missed payments) download their Paybill/Till statement
# from Safaricom's M-Pesa business portal and upload it. Money paid in is matched to tenants the same way
# live payments are: by account number, then by phone.

STATEMENT_MAX_BYTES = 5 * 1024 * 1024
STATEMENT_MAX_ROWS = 5000
STATEMENT_COLUMNS = {
    'receipt': ('receipt no.', 'receipt no', 'receipt', 'receipt number', 'transaction id', 'trans id'),
    'time': ('completion time', 'transaction time', 'completion date', 'date', 'time'),
    'details': ('details', 'description', 'narration'),
    'status': ('transaction status', 'status'),
    'paid_in': ('paid in', 'credit', 'amount', 'paid in (ksh)'),
    'party': ('other party info', 'other party', 'customer', 'payer'),
    'account': ('a/c no.', 'a/c no', 'account no.', 'account no', 'account number', 'bill ref', 'bill ref no'),
}
TIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%d-%m-%Y %H:%M:%S', '%d/%m/%Y %H:%M:%S', '%Y/%m/%d %H:%M:%S',
                '%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M', '%d/%m/%Y %H:%M', '%d.%m.%Y %H:%M:%S')


class StatementError(Exception):
    """A problem with the uploaded file as a whole, in words the landlord can act on."""


def _table(upload):
    name = (upload.name or '').lower()
    if name.endswith('.xls'):
        raise StatementError('That is an old-style Excel file (.xls). Open it in Excel and use Save As → '
                             '"Excel Workbook (.xlsx)", or download the statement as CSV, then upload that.')
    if name.endswith('.pdf'):
        raise StatementError("PDF statements can't be read. On the M-Pesa business portal, download the "
                             'statement as Excel or CSV instead.')
    if name.endswith('.csv'):
        return list(csv.reader(io.StringIO(upload.read().decode('utf-8-sig', errors='replace'))))
    if name.endswith(('.xlsx', '.xlsm')):
        from openpyxl import load_workbook

        try:
            sheet = load_workbook(upload, read_only=True, data_only=True).worksheets[0]
        except Exception as exc:
            raise StatementError("That file couldn't be opened as an Excel workbook.") from exc
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    raise StatementError('Upload the statement as an Excel file (.xlsx) or a CSV file.')


def _heading(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().lower()


def _heading_row(table):
    """(row index, {column: field}) of the table's heading row. The portal puts account details above it."""
    lookup = {alias: field for field, aliases in STATEMENT_COLUMNS.items() for alias in aliases}
    for index, row in enumerate(table[:40]):
        columns = {}
        for i, heading in enumerate(row):
            field = lookup.get(_heading(heading))
            if field and field not in columns.values():
                columns[i] = field
        if {'receipt', 'time', 'paid_in'} <= set(columns.values()):
            return index, columns
    return None


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, datetime.datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value).strip()


def read_statement(upload):
    """Money paid in, from an M-Pesa business statement (.xlsx or .csv), as JSON-friendly rows."""
    table = _table(upload)
    found = _heading_row(table)
    if found is None:
        raise StatementError("This doesn't look like an M-Pesa statement: no \"Receipt No.\", \"Completion "
                             'Time" and "Paid In" columns were found. Download the statement from the M-Pesa '
                             'business portal as Excel or CSV and upload it as it is.')
    index, columns = found
    body = table[index + 1:]
    if len(body) > STATEMENT_MAX_ROWS:
        raise StatementError(f'Up to {STATEMENT_MAX_ROWS:,} rows at a time. Download a shorter date range.')
    rows = []
    for line, raw in enumerate(body, start=index + 2):
        values = {field: _cell(raw[i]) if i < len(raw) else '' for i, field in columns.items()}
        receipt = values.get('receipt', '').upper()
        if not receipt or not re.fullmatch(r'[A-Z0-9]{8,12}', receipt):
            continue  # totals, blank lines, footers
        amount = _amount(values.get('paid_in'))
        if not amount or amount <= 0:
            continue  # money paid out, charges and transfers
        status = values.get('status', '').lower()
        if status and status not in ('completed', 'complete', 'success', 'successful'):
            continue
        phone, name = _party(values.get('party', ''), values.get('details', ''))
        rows.append({
            'line': line, 'receipt': receipt, 'time': values.get('time', ''), 'amount': str(amount),
            'account': (values.get('account') or _account_from_details(values.get('details', ''))).strip()[:100],
            'phone': phone, 'name': name, 'details': values.get('details', '')[:200],
        })
    if not rows:
        raise StatementError('No payments received were found in this statement. Check the date range you '
                             'downloaded includes payments from tenants.')
    return rows


def _amount(value):
    try:
        return Decimal(str(value).replace(',', '').replace('KES', '').replace('Ksh', '').strip() or '0')
    except InvalidOperation:
        return None


def _party(party, details):
    """("254712345678", "JOHN DOE") from "254712345678 - JOHN DOE" (numbers are often masked)."""
    text = party or re.sub(r'^.*?\bfrom\s+', '', details, flags=re.I).split(' Acc')[0]
    number, _, name = text.partition(' - ')
    return number.strip()[:30], name.strip()[:100]


def _account_from_details(details):
    match = re.search(r'\bAcc(?:ount)?\.?\s*(?:No\.?)?\s*[:#]?\s*(\S+)', details, re.I)
    return match.group(1) if match else ''


def parse_statement_time(value):
    value = (value or '').strip()
    for fmt in TIME_FORMATS:
        try:
            return timezone.make_aware(datetime.datetime.strptime(value, fmt))
        except ValueError:
            continue
    return None


def _house_key(value):
    return re.sub(r'[^A-Z0-9]', '', (value or '').upper()) or None


def check_statement(rows, account):
    """Each row with what will happen to it: 'match' (a tenant), 'unmatched' (to assign by hand),
    'recorded' (already in Rentipay) or 'error'."""
    from rentals.models import Lease

    from .models import C2BPayment, Payment
    from .mpesa import match_lease

    receipts = [r['receipt'] for r in rows]
    known = set(Payment.all_objects.filter(method=Payment.Method.MPESA, reference__in=receipts)
                .values_list('reference', flat=True))
    known |= set(C2BPayment.objects.filter(trans_id__in=receipts).values_list('trans_id', flat=True))
    # Tenants often type their house number instead of the account number. Live payments don't match on
    # it (nobody checks them first), but every statement row is shown before it's recorded.
    by_house = {}
    for lease in Lease.objects.filter(is_active=True, unit__property__owner=account.owner).select_related('unit'):
        by_house.setdefault(_house_key(lease.unit.label), []).append(lease)
    leases = {}
    seen, checked = set(), []
    for row in rows:
        item = {**row, 'state': '', 'lease': None, 'error': '', 'by_house': False}
        when = parse_statement_time(row['time'])
        if row['receipt'] in known or row['receipt'] in seen:
            item['state'] = 'recorded'
        elif when is None:
            item['state'], item['error'] = 'error', f'Couldn\'t read the date "{row["time"]}".'
        else:
            lease = match_lease(account, row['account'], row['phone'])
            if lease is None and len(by_house.get(_house_key(row['account']), [])) == 1:
                lease, item['by_house'] = by_house[_house_key(row['account'])][0], True
            item['state'] = 'match' if lease else 'unmatched'
            if lease:
                leases[lease.pk] = lease
                item['lease'] = lease
        seen.add(row['receipt'])
        checked.append(item)
    # One query for display details, rather than one per row.
    if leases:
        full = Lease.objects.select_related('tenant', 'unit__property').in_bulk(list(leases))
        for item in checked:
            if item['lease'] is not None:
                item['lease'] = full[item['lease'].pk]
    return checked


def apply_statement(checked, account, actor, send_receipts=False):
    """Record the payments from a checked statement. Returns (matched, unmatched) counts."""
    from audit import utils as audit

    from .models import C2BPayment
    from .mpesa import apply_c2b_payment

    matched = unmatched = 0
    with transaction.atomic():
        for item in checked:
            if item['state'] not in ('match', 'unmatched'):
                continue
            c2b, created = C2BPayment.objects.get_or_create(trans_id=item['receipt'], defaults={
                'source': C2BPayment.Source.STATEMENT, 'account': account,
                'trans_type': 'Statement', 'trans_time': parse_statement_time(item['time']),
                'amount': Decimal(item['amount']), 'shortcode': account.pay_number,
                'bill_ref': item['account'], 'msisdn': item['phone'], 'payer_name': item['name'],
                'raw': {k: item[k] for k in ('line', 'receipt', 'time', 'amount', 'account', 'phone', 'name',
                                              'details')},
            })
            if not created:
                continue  # arrived live while the preview was open
            if item['state'] == 'match':
                c2b.lease = item['lease']
                apply_c2b_payment(c2b, send_receipt=send_receipts)
                c2b.save()
                matched += 1
            else:
                unmatched += 1
        account.statement_imported_at = timezone.now()
        account.save(update_fields=['statement_imported_at', 'updated_at'])
        audit.record(owner=account.owner, actor=actor, action='payment.statement_uploaded',
                     summary=f'Uploaded an M-Pesa statement: {matched} payment{"s" if matched != 1 else ""} '
                             f'recorded, {unmatched} to assign.')
    return matched, unmatched

