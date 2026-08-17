import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


def _get_fernet():
    """Fernet instance derived from SECRET_KEY, so encryption works with zero extra config."""
    derived_key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(derived_key)


class EncryptedCharField(models.CharField):
    """A CharField that is transparently encrypted at rest using Fernet."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return _get_fernet().encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        try:
            return _get_fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            # Value written before encryption was introduced. It reads as
            # plaintext until the row is next saved, at which point
            # get_prep_value encrypts it.
            return value
