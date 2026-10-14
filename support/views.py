from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext
from django.views.generic import ListView

from accounts import utils as ratelimit
from audit import utils as audit
from rentals.utils import OwnedMixin
from rentals.utils import tenant_page
from rentals.models import Tenant

from . import utils as notify
from .forms import ComplaintForm, ContactForm, LandlordReplyForm, ReplyForm
from .models import Complaint, ComplaintMessage

TENANT_MESSAGES_PER_DAY = 20
CONTACTS_PER_IP_PER_HOUR = 5


# --- Tenant side (private portal link, no login) ----------------------------------------------

def _tenant(token):
    return get_object_or_404(Tenant.objects.select_related('owner'), portal_token=token)


def _tenant_limited(request, tenant):
    if ratelimit.hit('tenant-msg', tenant.pk, 24 * 60 * 60) > TENANT_MESSAGES_PER_DAY:
        messages.error(request, gettext("You've sent a lot of messages today. Please try again tomorrow."))
        return True
    return False


@tenant_page
def tenant_messages(request, token):
    tenant = _tenant(token)
    form = ComplaintForm(request.POST or None)
    if request.method == 'POST' and not _tenant_limited(request, tenant) and form.is_valid():
        with transaction.atomic():
            complaint = form.save(commit=False)
            complaint.owner = tenant.owner
            complaint.tenant = tenant
            complaint.lease = tenant.leases.filter(is_active=True).select_related('unit').first()
            complaint.save()
            ComplaintMessage.objects.create(complaint=complaint, author=ComplaintMessage.Author.TENANT,
                                            body=form.cleaned_data['message'])
        notify.landlord_new_message(complaint, form.cleaned_data['message'], is_new=True)
        messages.success(request, gettext('Sent. Your landlord has been notified, and you will get an SMS when they reply.'))
        return redirect('support:tenant_complaint', token=token, pk=complaint.pk)
    return render(request, 'support/tenant_messages.html', {
        'tenant': tenant, 'form': form, 'complaints': tenant.complaints.all(),
    })


@tenant_page
def tenant_complaint(request, token, pk):
    tenant = _tenant(token)
    complaint = get_object_or_404(tenant.complaints, pk=pk)
    form = ReplyForm(request.POST or None)
    if request.method == 'POST' and not _tenant_limited(request, tenant) and form.is_valid():
        ComplaintMessage.objects.create(complaint=complaint, author=ComplaintMessage.Author.TENANT,
                                        body=form.cleaned_data['body'])
        complaint.unread_by_landlord = True
        if complaint.status == Complaint.Status.RESOLVED:
            complaint.status, complaint.resolved_at = Complaint.Status.OPEN, None  # re-opened by the tenant
        complaint.save()
        notify.landlord_new_message(complaint, form.cleaned_data['body'], is_new=False)
        messages.success(request, gettext('Reply sent.'))
        return redirect('support:tenant_complaint', token=token, pk=pk)
    return render(request, 'support/tenant_complaint.html', {
        'tenant': tenant, 'complaint': complaint, 'form': form,
        'thread': complaint.messages.all(),
    })


# --- Landlord side -----------------------------------------------------------------------------

class ComplaintList(OwnedMixin, ListView):
    model = Complaint
    template_name = 'support/complaint_list.html'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related('tenant', 'lease__unit__property')
        status = self.request.GET.get('status', 'active')
        if status == 'active':
            return qs.exclude(status=Complaint.Status.RESOLVED)
        if status in Complaint.Status.values:
            return qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        return super().get_context_data(status=self.request.GET.get('status', 'active'), **kwargs)


@login_required
def complaint_detail(request, pk):
    complaint = get_object_or_404(
        Complaint.objects.select_related('tenant', 'lease__unit__property'), pk=pk, owner=request.user,
    )
    if complaint.unread_by_landlord and request.method == 'GET':
        complaint.unread_by_landlord = False
        complaint.save(update_fields=['unread_by_landlord'])
    form = LandlordReplyForm(request.POST or None, initial={'status': complaint.status})
    if request.method == 'POST' and form.is_valid():
        body, status = form.cleaned_data['body'], form.cleaned_data['status']
        if body:
            ComplaintMessage.objects.create(complaint=complaint, author=ComplaintMessage.Author.LANDLORD, body=body)
            notify.tenant_reply(complaint)
        if status != complaint.status:
            old = complaint.get_status_display()
            complaint.set_status(status)
            audit.record(owner=request.user, actor=request.user, lease=complaint.lease, action='complaint.status',
                         summary=f'Marked complaint "{complaint.subject}" from {complaint.tenant} as '
                                 f'{complaint.get_status_display().lower()}.',
                         changes={'Status': (old, complaint.get_status_display())})
        else:
            complaint.save(update_fields=['updated_at'])
        messages.success(request, 'Reply sent. The tenant has been notified by SMS.' if body else 'Status updated.')
        return redirect('support:complaint_detail', pk=pk)
    return render(request, 'support/complaint_detail.html', {
        'complaint': complaint, 'thread': complaint.messages.all(), 'form': form,
    })


# --- Contact the people running the site ---------------------------------------------------------

def contact(request):
    user = request.user if request.user.is_authenticated else None
    initial = {'name': user.get_full_name() or user.username, 'email': user.email} if user else {}
    form = ContactForm(request.POST or None, initial=initial)
    if request.method == 'POST':
        if ratelimit.hit('contact', ratelimit.client_ip(request), 60 * 60) > CONTACTS_PER_IP_PER_HOUR:
            return ratelimit.too_many(request, 60)
        if form.is_valid():
            if form.cleaned_data.get('website'):  # honeypot filled: pretend it worked
                messages.success(request, "Thanks, we've got your message and will reply by email.")
                return redirect('support:contact')
            msg = form.save(commit=False)
            msg.user = user
            msg.page = (request.POST.get('page') or request.META.get('HTTP_REFERER', ''))[:300]
            msg.save()
            notify.support_message(msg)
            messages.success(request, "Thanks, we've got your message and will reply by email.")
            return redirect('support:contact')
    template = 'support/contact_app.html' if user else 'support/contact.html'
    return render(request, template, {'form': form, 'page': request.GET.get('from', '')})
