from django.conf import settings
from django.db import models


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    email_verified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'Profile of {self.user}'

    @property
    def email_verified(self):
        return self.email_verified_at is not None


def profile_for(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile
