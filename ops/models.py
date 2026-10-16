import re

from django.core.cache import cache
from django.db import models


class JobRun(models.Model):
    """Last run of each scheduled job, so restarts don't skip or repeat daily jobs."""

    name = models.CharField(max_length=60, unique=True)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_finished_at = models.DateTimeField(null=True, blank=True)
    last_ok = models.BooleanField(default=True)
    last_output = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class SiteSettings(models.Model):
    """Business details shown on the site (legal pages, help page, footer), edited by an administrator in
    Settings → Business details. One row."""

    legal_name = models.CharField('registered business name', max_length=200, blank=True,
                                  help_text='As registered, e.g. "Rentipay Ltd". Shown on the terms and privacy policy.')
    address = models.TextField('postal or physical address', blank=True)
    privacy_email = models.EmailField('email for privacy and data requests', blank=True)
    support_phone = models.CharField('support phone', max_length=20, blank=True,
                                     help_text='Shown on the help page, e.g. 0712 345 678.')
    whatsapp = models.CharField('WhatsApp number', max_length=20, blank=True,
                                help_text='Landlords can message you here from the help page and footer.')
    facebook = models.URLField(blank=True)
    instagram = models.URLField(blank=True)
    x = models.URLField('X (Twitter)', blank=True)
    tiktok = models.URLField('TikTok', blank=True)
    linkedin = models.URLField('LinkedIn', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    CACHE_KEY = 'ops:site-settings'
    SOCIAL = ['facebook', 'instagram', 'x', 'tiktok', 'linkedin']

    class Meta:
        verbose_name = verbose_name_plural = 'business details'

    def __str__(self):
        return 'Business details'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    @classmethod
    def load(cls):
        row = cache.get(cls.CACHE_KEY)
        if row is None:
            row = cls.objects.get_or_create(pk=1)[0]
            cache.set(cls.CACHE_KEY, row, 300)
        return row

    @property
    def whatsapp_url(self):
        digits = re.sub(r'\D', '', re.sub(r'^\s*0', '254', self.whatsapp))
        return f'https://wa.me/{digits}' if digits else ''

    def social_links(self):
        links = [(name, getattr(self, name)) for name in self.SOCIAL if getattr(self, name)]
        return links + ([('whatsapp', self.whatsapp_url)] if self.whatsapp_url else [])

    def legal(self):
        """What the terms and privacy policy show: these details, else the server's LEGAL defaults."""
        from django.conf import settings

        return {**settings.LEGAL, 'ENTITY': self.legal_name or settings.LEGAL['ENTITY'],
                'ADDRESS': self.address or settings.LEGAL['ADDRESS'],
                'EMAIL': self.privacy_email or settings.LEGAL['EMAIL']}
