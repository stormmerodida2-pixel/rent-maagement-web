from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from rentals.crud_views import NextUrlMixin
from rentals.mixins import OwnedMixin

from . import sending
from .forms import AnnouncementForm
from .models import Announcement, PlatformNotice


class AnnouncementList(OwnedMixin, ListView):
    model = Announcement
    template_name = 'announcements/list.html'
    paginate_by = 30


class AnnouncementCreate(OwnedMixin, NextUrlMixin, CreateView):
    model = Announcement
    form_class = AnnouncementForm
    template_name = 'announcements/form.html'

    def get_success_url(self):
        return reverse('announcements:detail', args=[self.object.pk])


class AnnouncementUpdate(OwnedMixin, NextUrlMixin, UpdateView):
    model = Announcement
    form_class = AnnouncementForm
    template_name = 'announcements/form.html'

    def get_queryset(self):
        return super().get_queryset().filter(status=Announcement.Status.DRAFT)  # sent ones are history

    def get_success_url(self):
        return reverse('announcements:detail', args=[self.object.pk])


@login_required
def announcement_detail(request, pk):
    a = get_object_or_404(Announcement, pk=pk, owner=request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'send' and a.status == Announcement.Status.DRAFT:
            if a.scheduled_for:
                a.status = Announcement.Status.SCHEDULED
                a.save(update_fields=['status'])
                messages.success(request, f'Scheduled for {a.scheduled_for:%d %b %Y, %H:%M}.')
            else:
                stats = sending.send(a)
                messages.success(request, f"Sent to {stats['recipients']} tenant(s)."
                                 + (f" {stats['failed']} SMS failed: see the SMS log." if stats['failed'] else ''))
        elif action == 'cancel' and a.status == Announcement.Status.SCHEDULED:
            a.status = Announcement.Status.DRAFT
            a.save(update_fields=['status'])
            messages.success(request, 'Unscheduled. It is a draft again, so you can edit it.')
        elif action == 'unpublish' and a.status == Announcement.Status.SENT:
            a.status = Announcement.Status.CANCELLED
            a.save(update_fields=['status'])
            messages.success(request, "Removed from tenants' pages. SMS already sent can't be recalled.")
        elif action == 'delete' and a.status == Announcement.Status.DRAFT:
            a.delete()
            messages.success(request, 'Draft deleted.')
            return redirect('announcements:list')
        return redirect('announcements:detail', pk=a.pk)

    recipients = a.recipient_leases() if a.status in (Announcement.Status.DRAFT, Announcement.Status.SCHEDULED) else []
    deliveries = a.deliveries.select_related('tenant', 'lease__unit', 'sms') if a.sent_at else []
    sms_stats = {}
    for d in deliveries:
        if d.sms:
            sms_stats[d.sms.status] = sms_stats.get(d.sms.status, 0) + 1
    return render(request, 'announcements/detail.html', {
        'a': a, 'recipients': recipients, 'deliveries': deliveries, 'sms_stats': sms_stats,
        'sms_text': a.sms_text(), 'sms_parts': a.sms_parts(),
        'estimated_cost': a.estimated_cost(len(recipients)) if recipients else 0,
    })


@login_required
@require_POST
def dismiss_notice(request, pk):
    notice = get_object_or_404(PlatformNotice, pk=pk)
    notice.dismissed_by.add(request.user)
    return redirect(request.POST.get('next') or 'rentals:dashboard')
