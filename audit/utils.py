"""Record audit entries. Call from views and jobs right after the change is saved."""

from .models import AuditEntry


# --- Recording ---------------------------------------------------------------------------------------

# Record audit entries. Call from views and jobs right after the change is saved.

def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def record(*, owner, action, summary, actor=None, lease=None, changes=None):
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
