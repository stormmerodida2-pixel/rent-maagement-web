"""Snap your rent book: photos of a handwritten rent book (or a printed list) are read by Claude into the same
rows an Excel import gives, so they go through the same preview, checks and confirmation. Nothing is saved until
the landlord confirms the preview."""

import base64
import json
import logging

from django.conf import settings

from .utils import ImportError_

logger = logging.getLogger(__name__)

MODEL = 'claude-opus-5-5'
MAX_PHOTOS = 5
FIELDS = ['unit', 'type', 'rent', 'tenant', 'phone', 'rent_day', 'moved_in', 'deposit', 'balance']

SCHEMA = {
    'type': 'object',
    'properties': {
        'rows': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'unit': {'type': 'string', 'description': 'House or room number/label, e.g. "A4", "12".'},
                    'type': {'type': 'string', 'description': 'Only if written: Single room, Double room, One bedroom, '
                                                              'Two bedroom, Three bedroom or Shop. Else "".'},
                    'rent': {'type': 'string', 'description': 'Monthly rent in KES, digits only, e.g. "12000".'},
                    'tenant': {'type': 'string', 'description': 'Tenant name as written, or "" if vacant.'},
                    'phone': {'type': 'string', 'description': 'Phone number as written, or "".'},
                    'rent_day': {'type': 'string', 'description': 'Day of the month rent is due (1-31), or "".'},
                    'moved_in': {'type': 'string', 'description': 'Move-in date as YYYY-MM-DD if written, else "".'},
                    'deposit': {'type': 'string', 'description': 'Deposit paid in KES, digits only, or "".'},
                    'balance': {'type': 'string', 'description': 'What the tenant owes now in KES, digits only '
                                                                 '(minus sign if in credit), or "" if not shown.'},
                    'unclear': {'type': 'boolean', 'description': 'True if any value in this row was hard to read.'},
                },
                'required': FIELDS + ['unclear'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['rows'],
    'additionalProperties': False,
}

PROMPT = """These are photos of a Kenyan landlord's rent book (handwritten or printed): one row per house or room.

Read every house and give one row each, in the order they appear across the photos. Copy what is written; never \
invent values. Leave a field as "" when it isn't written or you can't read it, and set "unclear" to true for any \
row with a value you weren't sure of. Amounts are Kenyan shillings: write digits only (12,000 or 12k becomes \
"12000"). If the book shows several months of payments, work out what each tenant owes now only when it is clear \
(e.g. a balance or arrears column); otherwise leave "balance" as "". Dates become YYYY-MM-DD; Kenyan dates are \
day/month/year. If the same house appears in more than one photo, give it once. Ignore totals, signatures and notes \
that are not about a specific house."""


def available():
    return bool(getattr(settings, 'ANTHROPIC_API_KEY', ''))


def read_rent_book(photos):
    """Rows like read_spreadsheet()'s ({'line': n, 'unit': ..., ...}) from up to MAX_PHOTOS JPEG photos (bytes).
    Rows Claude marked as hard to read carry 'unclear': True. Raises ImportError_ with a plain message."""
    import anthropic

    if not available():
        raise ImportError_('Reading rent book photos isn\'t switched on yet. Use the Excel template instead.')
    content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                            'data': base64.standard_b64encode(photo).decode('ascii')}}
               for photo in photos[:MAX_PHOTOS]]
    content.append({'type': 'text', 'text': PROMPT})
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=180.0)
    try:
        response = client.beta.messages.create(
            model=MODEL, max_tokens=16000,
            betas=['server-side-fallback-2026-07-01'], fallbacks='default',  # a declined request is retried
            output_config={'effort': 'high', 'format': {'type': 'json_schema', 'schema': SCHEMA}},
            messages=[{'role': 'user', 'content': content}],
        )
    except anthropic.RateLimitError as exc:
        logger.warning('Rent book reading rate limited: %s', exc)
        raise ImportError_('Too many photos are being read right now. Please try again in a minute.') from exc
    except anthropic.BadRequestError as exc:
        logger.warning('Rent book photos rejected: %s', exc)
        raise ImportError_("Those photos couldn't be read. Try clearer photos, one page each.") from exc
    except anthropic.APIStatusError as exc:
        logger.error('Rent book reading failed (%s): %s', exc.status_code, exc)
        raise ImportError_("We couldn't read the photos just now. Please try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        logger.error('Rent book reading: no connection: %s', exc)
        raise ImportError_("We couldn't read the photos just now. Please try again shortly.") from exc

    if response.stop_reason == 'refusal':
        raise ImportError_("Those photos couldn't be read. Make sure they show a rent book or list of tenants.")
    if response.stop_reason == 'max_tokens':
        raise ImportError_('That is a lot to read at once. Send fewer pages at a time.')
    text = next((b.text for b in response.content if b.type == 'text'), '')
    try:
        found = json.loads(text)['rows']
    except (ValueError, KeyError, TypeError) as exc:
        logger.error('Rent book reading returned unexpected output: %r', text[:300])
        raise ImportError_("We couldn't read the photos just now. Please try again shortly.") from exc
    rows = []
    for line, row in enumerate(found, start=1):
        clean = {field: str(row.get(field) or '').strip() for field in FIELDS}
        if not any(clean.values()):
            continue
        rows.append({'line': line, **clean, 'unclear': bool(row.get('unclear'))})
    if not rows:
        raise ImportError_("No houses could be read from those photos. Try clearer photos, one page each.")
    return rows
