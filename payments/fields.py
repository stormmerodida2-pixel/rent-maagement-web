import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

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
