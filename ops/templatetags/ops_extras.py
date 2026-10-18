from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from ops.models import WorkItem
from ops.queries import ACTIVE, STALE_AFTER

register = template.Library()

S = WorkItem.Status

STATUS_LABELS = dict(S.choices)

CLOCK = mark_safe(
    '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
)


def _span(delta):
    seconds = abs(delta.total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


@register.filter
def status_label(status):
    return STATUS_LABELS.get(status, status)


@register.filter
def ago(dt):
    return f"{_span(timezone.now() - dt)} ago"


@register.simple_tag
def sla_pill(item):
    if item.status not in ACTIVE:
        return ""
    left = item.sla_due_at - timezone.now()
    if left.total_seconds() < 0:
        return format_html('<span class="pill breached">{}Overdue by {}</span>', CLOCK, _span(left))
    total = WorkItem.SLA[item.priority]
    css = "soon" if left < total / 4 else "ok"
    return format_html('<span class="pill {}">{}{} left</span>', css, CLOCK, _span(left))


@register.simple_tag
def stale_pill(item):
    if item.status in ACTIVE and item.last_activity_at < timezone.now() - STALE_AFTER:
        return format_html('<span class="pill stale">Quiet for {}</span>', _span(timezone.now() - item.last_activity_at))
    return ""


@register.filter
def event_sentence(event):
    t, old, new = event.type, event.old_value, event.new_value
    if t == "created":
        return "raised this item"
    if t == "claimed":
        return "claimed it and started work"
    if t == "released":
        return "released it back to the team queue"
    if t == "unassigned":
        return f"unassigned {old}"
    if t == "status_changed":
        return f"moved it from {STATUS_LABELS.get(old, old)} → {STATUS_LABELS.get(new, new)}"
    if t == "priority_changed":
        return f"changed priority {old} → {new}"
    if t == "handed_off":
        return f"handed it off from {old} → {new}"
    if t == "edited":
        return f"changed {old} → {new}"
    if t == "commented":
        return "commented"
    return t




@register.simple_tag(takes_context=True)
def query_with(context, **changes):
    params = context["request"].GET.copy()
    for key, value in changes.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"
