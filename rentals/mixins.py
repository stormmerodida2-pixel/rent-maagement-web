from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import ProtectedError
from django.shortcuts import redirect


class OwnedMixin(LoginRequiredMixin):
    """Limit a view's objects to the signed-in landlord's own records.

    Set `owner_path` to the lookup from the model to its landlord, e.g. 'unit__property__owner'.
    Objects belonging to someone else 404, exactly like ones that don't exist.
    """

    owner_path = 'owner'
    form_takes_owner = True

    def get_queryset(self):
        return super().get_queryset().filter(**{self.owner_path: self.request.user})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if self.form_takes_owner:
            kwargs['owner'] = self.request.user
        return kwargs


class SafeDeleteMixin:
    """Refuse to delete records that payments or leases still depend on, with a friendly message."""

    protected_message = "This can't be deleted because other records depend on it."

    def form_valid(self, form):
        try:
            response = super().form_valid(form)
        except ProtectedError:
            messages.error(self.request, self.protected_message)
            return redirect(self.request.path)
        messages.success(self.request, f'Deleted {self.object}.')
        return response
