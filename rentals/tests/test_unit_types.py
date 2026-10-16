"""Units listed in counting order (A1, A2 … A10), and the four unit types."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import Property, Unit
from ..utils import unit_type_from_text


class UnitOrderTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.client.force_login(self.owner)

    def test_new_property_units_count_up(self):
        self.client.post(reverse('rentals:property_create'), {
            'name': 'Riverside', 'address': '', 'water_rate': '', 'unit_prefix': 'A', 'unit_count': 22,
            'unit_type': 'one_bedroom', 'unit_rent': '15000',
        })
        prop = Property.objects.get()
        labels = list(prop.units.values_list('label', flat=True))
        self.assertEqual(labels, [f'A{i}' for i in range(1, 23)])
        self.assertEqual(set(prop.units.values_list('unit_type', 'bedrooms')), {('one_bedroom', 1)})
        page = self.client.get(reverse('rentals:property_detail', args=[prop.pk])).content.decode()
        self.assertLess(page.index('<strong>A2</strong>'), page.index('<strong>A10</strong>'))

    def test_labels_added_by_hand_sort_naturally(self):
        prop = Property.objects.create(owner=self.owner, name='Riverside')
        for label in ['House 10', 'House 2', 'house 1', 'Shop 3']:
            Unit.objects.create(property=prop, label=label, default_rent=1000)
        self.assertEqual(list(prop.units.values_list('label', flat=True)), ['house 1', 'House 2', 'House 10', 'Shop 3'])


class UnitTypeTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('landlord', password='pw')
        self.client.force_login(self.owner)
        self.prop = Property.objects.create(owner=self.owner, name='Riverside')

    def test_four_types_with_bedrooms_from_the_type(self):
        self.assertEqual([label for _, label in Unit.Type.choices],
                         ['Single room', 'One bedroom', 'Two bedroom', 'Shop'])
        unit = Unit.objects.create(property=self.prop, label='S1', unit_type='shop', default_rent=8000)
        self.assertEqual(unit.bedrooms, 0)
        unit.unit_type = 'two_bedroom'
        unit.save(update_fields=['unit_type'])
        unit.refresh_from_db()
        self.assertEqual(unit.bedrooms, 2)

    def test_unit_form_offers_only_the_four(self):
        page = self.client.get(reverse('rentals:unit_create', args=[self.prop.pk]))
        for label in ('Single room', 'One bedroom', 'Two bedroom', 'Shop'):
            self.assertContains(page, f'>{label}</option>')
        self.assertNotContains(page, 'Bungalow')
        self.client.post(reverse('rentals:unit_create', args=[self.prop.pk]),
                         {'label': 'B1', 'unit_type': 'bungalow', 'default_rent': '9000'})
        self.assertFalse(Unit.objects.filter(label='B1').exists())

    def test_reading_types_from_spreadsheets(self):
        for text, expected in [('Bedsitter', 'single'), ('Single Room', 'single'), ('1-bedroom', 'one_bedroom'),
                               ('One bedroom', 'one_bedroom'), ('2BR', 'two_bedroom'), ('2 bedrooms', 'two_bedroom'),
                               ('SHOP', 'shop'), ('Bungalow', None)]:
            self.assertEqual(unit_type_from_text(text), expected, text)
