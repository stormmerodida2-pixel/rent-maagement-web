import secrets

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from rentals.models import Tenant, Unit


class Listing(models.Model):
    """A vacant (or soon vacant) unit advertised on a public page that can be shared on WhatsApp."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='listings')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='listings')
    slug = models.SlugField(max_length=120, unique=True, editable=False)
    title = models.CharField(max_length=120, help_text='e.g. "Bright 2-bedroom near Ruaka town".')
    rent = models.DecimalField('monthly rent (KES)', max_digits=10, decimal_places=2)
    deposit = models.DecimalField('deposit (KES)', max_digits=10, decimal_places=2, default=0)
    available_from = models.DateField()
    description = models.TextField(blank=True, help_text='Water, parking, security, distance to the road or town…')
    contact_phone = models.CharField(
        'phone for enquiries', max_length=20, blank=True,
        help_text='Shown on the page with a Call button. Leave blank to only take applications.',
    )
    is_open = models.BooleanField(default=True)
    views = models.PositiveIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-is_open', '-created_at']

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(f'{self.unit.bedrooms}-bedroom {self.unit.property.name} {self.unit.label}')[:100]
            self.slug = f'{base}-{secrets.token_hex(2)}'
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('listings:public', args=[self.slug])

    def public_url(self):
        return settings.SITE_URL.rstrip('/') + self.get_absolute_url()

    def close(self):
        if self.is_open:
            self.is_open, self.closed_at = False, timezone.now()
            self.save(update_fields=['is_open', 'closed_at'])

    @property
    def cover(self):
        return self.photos.first()


class ListingPhoto(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name='photos')
    image = models.ImageField(upload_to='listings/%Y/%m/')
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['position', 'pk']


class Application(models.Model):
    """Someone interested in a listing. Approving it creates the tenant, ready for a lease."""

    class Status(models.TextChoices):
        NEW = 'new', 'New'
        CONTACTED = 'contacted', 'Contacted'
        APPROVED = 'approved', 'Approved'
        DECLINED = 'declined', 'Declined'

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name='applications')
    name = models.CharField('your name', max_length=120)
    phone = models.CharField('your phone', max_length=20)
    email = models.EmailField('email (optional)', blank=True)
    move_in = models.DateField('when do you want to move in?', null=True, blank=True)
    occupants = models.PositiveSmallIntegerField('how many people will live there?', null=True, blank=True)
    message = models.TextField('anything else? (optional)', blank=True, max_length=1000)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    tenant = models.ForeignKey(Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} for {self.listing}'
