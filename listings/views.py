"""Vacancy listings: landlords advertise vacant units; anyone can view a listing and apply."""

import datetime
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts import utils as ratelimit
from audit import utils as audit
from notifications.models import LandlordPreferences
from rentals.models import Lease, Tenant, Unit

from .forms import ApplicationForm, ListingForm
from .models import Application, Listing, ListingPhoto
from .utils import MAX_PHOTOS, notify_landlord, prepare_photo

APPLICATIONS_PER_IP_PER_DAY = 10


# --- Landlord ------------------------------------------------------------------------------------

@login_required
def listing_list(request):
    listings = (Listing.objects.filter(owner=request.user).select_related('unit__property')
                .annotate(apps=Count('applications'),
                          new_apps=Count('applications', filter=Q(applications__status=Application.Status.NEW)))
                .prefetch_related('photos'))
    # Units you could advertise: vacant now, or the tenant has given notice.
    let_units = Lease.objects.filter(is_active=True, end_date__isnull=True).values('unit')
    open_units = Listing.objects.filter(owner=request.user, is_open=True).values('unit')
    candidates = (Unit.objects.filter(property__owner=request.user).exclude(pk__in=let_units)
                  .exclude(pk__in=open_units).select_related('property').order_by('property__name', 'sort_key'))
    return render(request, 'listings/list.html', {'listings': listings, 'candidates': candidates})


def _save_photos(request, listing, form):
    files = form.cleaned_data.get('photos') or []
    room = MAX_PHOTOS - listing.photos.count()
    if len(files) > room:
        form.add_error('photos', f'You can add {room} more photo{"s" if room != 1 else ""} (up to {MAX_PHOTOS}).')
        return False
    prepared = []
    for upload in files:
        try:
            prepared.append(prepare_photo(upload))
        except ValidationError as exc:
            form.add_error('photos', exc)
            return False
    start = listing.photos.count()
    for i, content in enumerate(prepared):
        photo = ListingPhoto(listing=listing, position=start + i)
        photo.image.save(content.name, content, save=True)
    return True


@login_required
def listing_create(request, unit_pk):
    unit = get_object_or_404(Unit.objects.select_related('property'), pk=unit_pk, property__owner=request.user)
    moving_out = unit.leases.filter(is_active=True, end_date__isnull=False).first()
    available = moving_out.end_date + datetime.timedelta(days=1) if moving_out else datetime.date.today()
    prefs = LandlordPreferences.for_user(request.user)
    form = ListingForm(request.POST or None, request.FILES or None, initial={
        'title': f'{unit.get_unit_type_display()} at {unit.property.name}',
        'rent': unit.default_rent, 'deposit': unit.default_rent, 'available_from': available,
        'contact_phone': prefs.phone,
    })
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            listing = form.save(commit=False)
            listing.owner, listing.unit = request.user, unit
            listing.save()
            photos_ok = _save_photos(request, listing, form)
            if not photos_ok:
                transaction.set_rollback(True)
        if not photos_ok:  # rendered after the rollback, so the page can still query the database
            return render(request, 'listings/form.html', {'form': form, 'unit': unit})
        audit.record(owner=request.user, actor=request.user, action='listing.created',
                     summary=f'Advertised {unit}: {listing.title}.')
        messages.success(request, 'Your listing is live. Share the link on WhatsApp or Facebook.')
        return redirect('listings:detail', pk=listing.pk)
    return render(request, 'listings/form.html', {'form': form, 'unit': unit})


def _own_listing(request, pk):
    return get_object_or_404(Listing.objects.select_related('unit__property'), pk=pk, owner=request.user)


@login_required
def listing_detail(request, pk):
    """The landlord's view: share link, edit, photos and applications."""
    listing = _own_listing(request, pk)
    form = ListingForm(request.POST or None, request.FILES or None, instance=listing)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'close':
            listing.close()
            messages.success(request, 'Listing closed. Its page now says the place has been let.')
            return redirect('listings:detail', pk=pk)
        if action == 'reopen':
            listing.is_open, listing.closed_at = True, None
            listing.save(update_fields=['is_open', 'closed_at'])
            messages.success(request, 'Listing reopened.')
            return redirect('listings:detail', pk=pk)
        if action == 'delete_photo':
            listing.photos.filter(pk=request.POST.get('photo')).delete()
            return redirect('listings:detail', pk=pk)
        if form.is_valid():
            with transaction.atomic():
                form.save()
                if not _save_photos(request, listing, form):
                    transaction.set_rollback(True)
                else:
                    messages.success(request, 'Listing updated.')
                    return redirect('listings:detail', pk=pk)
    share_text = f'{listing.title}: KES {listing.rent:,.0f} a month. {listing.public_url()}'
    return render(request, 'listings/detail.html', {
        'listing': listing, 'form': form, 'photos': listing.photos.all(),
        'applications': listing.applications.select_related('tenant'),
        'whatsapp_url': 'https://wa.me/?text=' + quote(share_text),
        'max_photos': MAX_PHOTOS,
    })


@login_required
@require_POST
def application_action(request, pk):
    application = get_object_or_404(Application.objects.select_related('listing__unit__property'),
                                    pk=pk, listing__owner=request.user)
    action = request.POST.get('action')
    listing = application.listing
    if action in (Application.Status.CONTACTED, Application.Status.DECLINED, Application.Status.NEW):
        application.status = action
        application.save(update_fields=['status'])
        return redirect('listings:detail', pk=listing.pk)
    if action == 'approve':
        with transaction.atomic():
            tenant = application.tenant or Tenant.objects.create(
                owner=request.user, full_name=application.name, phone=application.phone, email=application.email,
            )
            application.status, application.tenant = Application.Status.APPROVED, tenant
            application.save(update_fields=['status', 'tenant'])
        audit.record(owner=request.user, actor=request.user, action='listing.application_approved',
                     summary=f'Approved {application.name} for {listing.unit} and added them as a tenant.')
        if Lease.objects.filter(unit=listing.unit, is_active=True).exists():
            messages.success(request, f'{tenant} has been added as a tenant. They can move in once the current tenant '
                                      'has moved out ("Finish move-out" on their page).')
            return redirect('listings:detail', pk=listing.pk)
        messages.success(request, f'{tenant} has been added as a tenant. Check their rent and details, then move them in.')
        return redirect(f"{reverse('rentals:lease_create')}?unit={listing.unit_id}&tenant={tenant.pk}")
    raise Http404


# --- Public -------------------------------------------------------------------------------------

def listing_public(request, slug):
    listing = get_object_or_404(Listing.objects.select_related('unit__property', 'owner'), slug=slug)
    form = ApplicationForm(request.POST or None, initial={'move_in': listing.available_from})
    if request.method == 'GET' and listing.is_open:
        Listing.objects.filter(pk=listing.pk).update(views=F('views') + 1)
    if request.method == 'POST' and listing.is_open:
        if form.data.get('website'):  # a bot filled in the hidden field
            return redirect(listing.get_absolute_url() + '?sent=1')
        ip = ratelimit.client_ip(request)
        if ratelimit.hit('listing-apply', ip, 24 * 60 * 60) > APPLICATIONS_PER_IP_PER_DAY:
            form.add_error(None, "You've sent a lot of applications today. Please try again tomorrow.")
        elif form.is_valid():
            if listing.applications.filter(phone=form.cleaned_data['phone']).exists():
                return redirect(listing.get_absolute_url() + '?sent=1')  # already applied: don't duplicate
            application = form.save(commit=False)
            application.listing = listing
            application.save()
            notify_landlord(application)
            return redirect(listing.get_absolute_url() + '?sent=1')
    return render(request, 'listings/public.html', {
        'listing': listing, 'form': form, 'photos': listing.photos.all(),
        'sent': request.GET.get('sent') == '1',
    })
