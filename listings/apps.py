from django.apps import AppConfig


class ListingsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'listings'
    verbose_name = 'Vacancy listings'

    def ready(self):
        from . import utils  # noqa: F401  (closes a listing when its unit gets a lease)
