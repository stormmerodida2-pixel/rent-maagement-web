"""Record audit entries. Call from views and jobs right after the change is saved."""

from contextvars import ContextVar

from .models import AuditEntry

# Set while someone the landlord let in (Settings → People with access) makes a change in the landlord's account,
# so the activity log names them rather than the landlord.
acting_user = ContextVar('acting_user', default=None)


# --- Recording ---------------------------------------------------------------------------------------

# Record audit entries. Call from views and jobs right after the change is saved.

def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def record(*, owner, action, summary, actor=None, lease=None, changes=None):
    real = acting_user.get()
    if real is not None and actor is not None and actor.pk == owner.pk:
        actor = real
    return AuditEntry.objects.create(
        owner=owner,
        actor=actor if actor is not None and actor.is_authenticated else None,
        lease=lease,
        action=action,
        summary=summary[:300],
        changes={k: [_plain(a), _plain(b)] for k, (a, b) in (changes or {}).items()},
    )


def form_changes(form):
    """{field: (before, after)} for an edited ModelForm, using the field labels."""
    out = {}
    for name in form.changed_data:
        field = form.fields.get(name)
        if field is None:
            continue
        before = form.initial.get(name)
        after = form.cleaned_data.get(name)
        if hasattr(field, 'queryset'):  # show names, not ids
            before = field.queryset.model._default_manager.filter(pk=before).first() if before else None
        if 'password' in name or name in ('consumer_key', 'consumer_secret', 'passkey'):
            before, after = '•••', '••• (changed)'
        out[str(field.label or name)] = (before, after)
    return out
