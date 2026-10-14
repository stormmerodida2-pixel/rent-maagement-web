"""Tests for the rentals app. make_lease is shared with other apps' tests."""

import datetime

from ..models import Lease, Property, Tenant, Unit


def make_lease(owner, label='A1', rent='15000', start=datetime.date(2026, 1, 15), **kwargs):
    prop, _ = Property.objects.get_or_create(owner=owner, name=f'{owner.username} Apartments')
    unit = Unit.objects.create(property=prop, label=label, default_rent=rent)
    tenant = Tenant.objects.create(owner=owner, full_name=f'Tenant {label}', phone='0712345678')
    return Lease.objects.create(unit=unit, tenant=tenant, start_date=start, monthly_rent=rent, **kwargs)
