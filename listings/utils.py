"""Helpers for vacancy listings: photos, the landlord's alert, and closing listings once a unit is let."""

import io
import logging
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db.models.signals import post_save
from django.urls import reverse
from PIL import Image, ImageOps, UnidentifiedImageError

from accounts.utils import is_verified
from rentals.models import Lease

logger = logging.getLogger(__name__)

MAX_PHOTOS = 8
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SIDE = 1600  # pixels; plenty for phones, and keeps pages fast on mobile data


# --- Photos ---------------------------------------------------------------------------------------

def prepare_photo(upload):
    """Check an uploaded photo and return a resized JPEG, without its EXIF data (which can include the
    GPS location where it was taken). Raises ValidationError for anything that isn't a usable image."""
    if upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError(f'{upload.name} is larger than 10 MB.')
    try:
        image = Image.open(upload)
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"{upload.name} isn't a photo we can read (use JPEG, PNG or WebP).") from exc
    image = ImageOps.exif_transpose(image)  # keep it the right way up once EXIF is gone
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    if image.mode not in ('RGB', 'L'):
        image = image.convert('RGB')
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=82, optimize=True)  # a fresh file: no EXIF carried over
    return ContentFile(out.getvalue(), name=f'{uuid.uuid4().hex}.jpg')


def prepare_logo(upload):
    """A landlord's logo for receipts: at most 600×200, PNG (keeps a transparent background), no EXIF."""
    if upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError(f'{upload.name} is larger than 10 MB.')
    try:
        image = Image.open(upload)
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError(f"{upload.name} isn't an image we can read (use PNG, JPEG or WebP).") from exc
    image = ImageOps.exif_transpose(image)
    image.thumbnail((600, 200))
    if image.mode not in ('RGB', 'RGBA', 'L', 'LA'):
        image = image.convert('RGBA')
    out = io.BytesIO()
    image.save(out, 'PNG', optimize=True)
    return ContentFile(out.getvalue(), name=f'{uuid.uuid4().hex}.png')


# --- Alerts ---------------------------------------------------------------------------------------

def notify_landlord(application):
    listing = application.listing
    owner = listing.owner
    if not (owner.email and is_verified(owner)):
        return
    url = settings.SITE_URL.rstrip('/') + reverse('listings:detail', args=[listing.pk])
    try:
        send_mail(
            subject=f'New application: {application.name} for {listing.unit}',
            message=(f'Hi {owner.first_name or "there"},\n\n{application.name} ({application.phone}) applied '
                     f'for {listing.title} ({listing.unit}).\n\n'
                     + (f'Move-in: {application.move_in:%d %b %Y}\n' if application.move_in else '')
                     + (f'People: {application.occupants}\n' if application.occupants else '')
                     + (f'\n"{application.message}"\n' if application.message else '')
                     + f'\nSee all applications and approve one here:\n{url}\n\n{settings.APP_NAME}'),
            from_email=None, recipient_list=[owner.email],
        )
    except Exception:
        logger.exception('Could not email landlord about application %s', application.pk)


# --- Closing listings once the unit is let ------------------------------------------------------------

def close_when_let(sender, instance, created, raw=False, **kwargs):
    if raw or not instance.is_active:
        return
    from .models import Listing

    for listing in Listing.objects.filter(unit_id=instance.unit_id, is_open=True):
        listing.close()


post_save.connect(close_when_let, sender=Lease, dispatch_uid='listings_close_when_let')
