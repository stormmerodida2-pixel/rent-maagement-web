"""Move-in and move-out inspections (photos room by room, side by side) and tenant documents (the tenancy agreement
to print and sign, then the signed copy and ID kept privately)."""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from audit import utils as audit

from .models import Inspection, InspectionPhoto, Lease, Tenant, TenantDocument

MAX_PHOTOS_PER_UPLOAD = 12
DOCUMENT_TYPES = {'application/pdf': 'pdf', 'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


def _lease(request, pk):
    return get_object_or_404(Lease.objects.select_related('tenant', 'unit__property'), pk=pk,
                             unit__property__owner=request.user)


def _rooms(inspection):
    """{room: [photos]} in the usual walk-through order."""
    order = {room: i for i, room in enumerate(InspectionPhoto.ROOMS)}
    rooms = {}
    for photo in sorted(inspection.photos.all(), key=lambda p: (order.get(p.room, 99), p.room, p.created_at)):
        rooms.setdefault(photo.room, []).append(photo)
    return rooms


# --- Inspections --------------------------------------------------------------------------------------------------

@login_required
def inspection(request, pk, kind):
    lease = _lease(request, pk)
    if kind not in Inspection.Kind.values:
        raise Http404
    insp = Inspection.objects.filter(lease=lease, kind=kind).first()
    if request.method == 'POST':
        from listings.utils import prepare_photo

        insp = insp or Inspection.objects.create(lease=lease, kind=kind, created_by=request.user)
        if 'notes' in request.POST:
            insp.notes = request.POST['notes'][:2000]
            if request.POST.get('done_on'):
                try:
                    insp.done_on = datetime.date.fromisoformat(request.POST['done_on'])
                except ValueError:
                    pass
            insp.save(update_fields=['notes', 'done_on'])
        room = (request.POST.get('room_other') or request.POST.get('room') or 'Other').strip()[:40]
        added, problems = 0, []
        for upload in request.FILES.getlist('photos')[:MAX_PHOTOS_PER_UPLOAD]:
            try:
                prepared = prepare_photo(upload)
            except ValidationError as exc:
                problems.append(' '.join(exc.messages))
                continue
            photo = InspectionPhoto(inspection=insp, room=room, note=request.POST.get('note', '').strip()[:200])
            photo.photo.save(prepared.name, prepared, save=True)
            added += 1
        if added:
            audit.record(owner=request.user, actor=request.user, lease=lease, action='inspection.photos',
                         summary=f'Added {added} {insp.get_kind_display().lower()} inspection photo'
                                 f'{"s" if added != 1 else ""} ({room}).')
            messages.success(request, f'Added {added} photo{"s" if added != 1 else ""} of the {room.lower()}.')
        for problem in problems:
            messages.error(request, problem)
        if not added and not problems and 'notes' in request.POST:
            messages.success(request, 'Saved.')
        return redirect('rentals:inspection', pk=lease.pk, kind=kind)
    return render(request, 'rentals/inspection.html', {
        'lease': lease, 'kind': kind, 'kind_label': Inspection.Kind(kind).label, 'inspection': insp,
        'rooms': _rooms(insp) if insp else {}, 'room_choices': InspectionPhoto.ROOMS,
        'other': Inspection.objects.filter(lease=lease).exclude(kind=kind).first(),
    })


@login_required
@require_POST
def inspection_photo_delete(request, pk):
    photo = get_object_or_404(InspectionPhoto.objects.select_related('inspection__lease'), pk=pk,
                              inspection__lease__unit__property__owner=request.user)
    insp = photo.inspection
    photo.photo.delete(save=False)
    photo.delete()
    messages.success(request, 'Photo removed.')
    return redirect('rentals:inspection', pk=insp.lease_id, kind=insp.kind)


def compare_rows(lease):
    """[(room, move-in photos, move-out photos)] for every room photographed either time."""
    by_kind = {i.kind: _rooms(i) for i in lease.inspections.prefetch_related('photos')}
    before, after = by_kind.get(Inspection.Kind.MOVE_IN, {}), by_kind.get(Inspection.Kind.MOVE_OUT, {})
    rooms = list(before) + [r for r in after if r not in before]
    return [(room, before.get(room, []), after.get(room, [])) for room in rooms]


@login_required
def inspection_compare(request, pk):
    lease = _lease(request, pk)
    return render(request, 'rentals/inspection_compare.html', {
        'lease': lease, 'rows': compare_rows(lease),
        'move_in': lease.inspections.filter(kind=Inspection.Kind.MOVE_IN).first(),
        'move_out': lease.inspections.filter(kind=Inspection.Kind.MOVE_OUT).first(),
    })


def tenant_inspection(request, token, pk):
    """The tenant sees the same photos from their rent page: nobody can later say they were never shown."""
    tenant = get_object_or_404(Tenant, portal_token=token)
    lease = get_object_or_404(tenant.leases.select_related('unit__property'), pk=pk)
    return render(request, 'rentals/tenant_inspection.html', {
        'lease': lease, 'tenant': tenant, 'rows': compare_rows(lease), 'tenant_view': True, 'token': token,
        'move_in': lease.inspections.filter(kind=Inspection.Kind.MOVE_IN).first(),
        'move_out': lease.inspections.filter(kind=Inspection.Kind.MOVE_OUT).first(),
    })


def tenant_inspection_photo(request, token, pk):
    photo = get_object_or_404(InspectionPhoto, pk=pk, inspection__lease__tenant__portal_token=token)
    return _file_response(photo.photo, 'image/jpeg')


def _file_response(field, content_type, download_name=None):
    response = FileResponse(field.open('rb'), content_type=content_type,
                            as_attachment=bool(download_name), filename=download_name or '')
    response['Cache-Control'] = 'private, max-age=3600'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


# --- Tenancy agreement and documents -------------------------------------------------------------------------------

@login_required
def agreement(request, pk):
    """A tenancy agreement filled in from the lease, to print and sign."""
    from accounts.models import profile_for
    from payments.models import MpesaAccount

    lease = _lease(request, pk)
    owner = request.user
    profile = profile_for(owner)
    account = MpesaAccount.objects.filter(owner=owner).first()
    return render(request, 'rentals/agreement.html', {
        'lease': lease, 'tenant': lease.tenant, 'landlord': profile.brand_name or owner.get_full_name() or 'The landlord',
        'landlord_phone': profile.phone or '', 'landlord_email': owner.email,
        'mpesa_account': account if account and account.has_pay_number else None,
        'clauses': [c.strip() for c in (profile.agreement_clauses or '').splitlines() if c.strip()],
        'today': datetime.date.today(), 'back_url': reverse('rentals:lease_detail', args=[lease.pk]),
    })


@login_required
@require_POST
def document_upload(request, pk):
    lease = _lease(request, pk)
    upload = request.FILES.get('file')
    kind = request.POST.get('kind') if request.POST.get('kind') in TenantDocument.Kind.values else 'other'
    if upload is None:
        messages.error(request, 'Choose a photo or PDF to upload.')
    elif upload.size > MAX_DOCUMENT_BYTES:
        messages.error(request, f'{upload.name} is larger than 10 MB.')
    else:
        head = upload.read(5)
        upload.seek(0)
        if head == b'%PDF-':
            content, content_type = upload, 'application/pdf'
        else:
            from listings.utils import prepare_photo

            try:
                content, content_type = prepare_photo(upload), 'image/jpeg'
            except ValidationError as exc:
                messages.error(request, ' '.join(exc.messages) + ' Upload a photo or a PDF.')
                return redirect('rentals:lease_detail', pk=lease.pk)
        doc = TenantDocument(tenant=lease.tenant, kind=kind, title=request.POST.get('title', '').strip()[:120],
                             content_type=content_type, uploaded_by=request.user)
        doc.file.save(f'{lease.tenant_id}-{kind}.{DOCUMENT_TYPES[content_type]}', content, save=True)
        audit.record(owner=request.user, actor=request.user, lease=lease, action='document.added',
                     summary=f'Added a document for {lease.tenant}: {doc.label}.')
        messages.success(request, f'Saved {doc.label.lower()}.')
    return redirect(reverse('rentals:lease_detail', args=[lease.pk]) + '#documents')


@login_required
def document_file(request, pk):
    doc = get_object_or_404(TenantDocument, pk=pk, tenant__owner=request.user)
    return _file_response(doc.file, doc.content_type)


@login_required
@require_POST
def document_delete(request, pk):
    doc = get_object_or_404(TenantDocument.objects.select_related('tenant'), pk=pk, tenant__owner=request.user)
    lease = doc.tenant.leases.order_by('-is_active', '-start_date').first()
    doc.file.delete(save=False)
    doc.delete()
    messages.success(request, 'Document removed.')
    return redirect(reverse('rentals:lease_detail', args=[lease.pk]) + '#documents' if lease else 'rentals:lease_list')
