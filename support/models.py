from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rentals.models import Lease, Tenant


class Complaint(models.Model):
    """A message from a tenant to their landlord (anything that isn't a repair)."""

    class Category(models.TextChoices):
        BILLING = 'billing', _('Rent or billing')
        NEIGHBOURS = 'neighbours', _('Noise or neighbours')
        SECURITY = 'security', _('Security')
        SERVICES = 'services', _('Water, power or garbage')
        OTHER = 'other', _('Something else')

    class Status(models.TextChoices):
        OPEN = 'open', _('Open')
        IN_PROGRESS = 'in_progress', _('In progress')
        RESOLVED = 'resolved', _('Resolved')

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='complaints')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='complaints')
    lease = models.ForeignKey(Lease, on_delete=models.SET_NULL, null=True, blank=True, related_name='complaints')
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    subject = models.CharField(max_length=150)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    unread_by_landlord = models.BooleanField(default=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.subject} ({self.tenant})'

    def set_status(self, status):
        self.status = status
        self.resolved_at = timezone.now() if status == self.Status.RESOLVED else None
        self.save(update_fields=['status', 'resolved_at', 'updated_at'])


class ComplaintMessage(models.Model):
    class Author(models.TextChoices):
        TENANT = 'tenant', 'Tenant'
        LANDLORD = 'landlord', 'Landlord'

    complaint = models.ForeignKey(Complaint, on_delete=models.CASCADE, related_name='messages')
    author = models.CharField(max_length=10, choices=Author.choices)
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class SupportMessage(models.Model):
    """A message to the people running the site, from the Contact page."""

    class Topic(models.TextChoices):
        QUESTION = 'question', 'Question'
        PROBLEM = 'problem', 'Something isn\'t working'
        FEEDBACK = 'feedback', 'Feedback or idea'
        ACCOUNT = 'account', 'My account or data'

    name = models.CharField(max_length=120)
    email = models.EmailField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='support_messages')
    topic = models.CharField(max_length=20, choices=Topic.choices, default=Topic.QUESTION)
    message = models.TextField(max_length=4000)
    page = models.CharField(max_length=300, blank=True, help_text='Where they were when they wrote.')
    handled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_topic_display()} from {self.name} <{self.email}>'
