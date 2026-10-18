from django.conf import settings
from django.db import models


class DismissedNudge(models.Model):
    """A dashboard suggestion the landlord hid ("Not now"); it can come back after `until`."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    key = models.CharField(max_length=40)
    until = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner', 'key'], name='one_dismissal_per_nudge')]


class TaxFiling(models.Model):
    """A landlord marked a month's Monthly Rental Income tax as filed and paid on iTax."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tax_filings')
    month = models.DateField(help_text='First day of the month the rent was received in.')
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text='Tax paid (KES).')
    reference = models.CharField('iTax payment reference (PRN)', max_length=40, blank=True)
    filed_on = models.DateField()

    class Meta:
        ordering = ['-month']
        constraints = [models.UniqueConstraint(fields=['owner', 'month'], name='one_filing_per_month')]
