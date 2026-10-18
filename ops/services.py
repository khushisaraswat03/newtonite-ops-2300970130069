"""
All writes to work items go through these functions.

Each one runs in a single database transaction that saves the change AND its
activity event together, so the history can never disagree with the item.
Views (API and HTML) only parse input and call these.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ActivityEvent, Comment, WorkItem
from .permissions import can_raise, check

E = ActivityEvent.Type


def _log(item, actor, type, old="", new="", reason="", at=None):
    """Record an activity event and mark the item as recently active. Call inside a transaction."""
    at = at or timezone.now()
    ActivityEvent.objects.create(
        item=item, actor=actor, type=type,
        old_value=str(old)[:200], new_value=str(new)[:200], reason=reason, created_at=at,
    )
    # A targeted UPDATE of one column, so it can't overwrite other fields changed concurrently.
    WorkItem.objects.filter(pk=item.pk).update(last_activity_at=at)
    item.last_activity_at = at


@transaction.atomic
def create_item(user, *, team, title, type, priority, description="", due_date=None):
    if not can_raise(user):
        raise PermissionDenied("Viewers cannot raise items.")

    title = (title or "").strip()
    if not title:
        raise ValidationError({"title": "Title is required."})
    if type not in WorkItem.Type.values:
        raise ValidationError({"type": f"Must be one of {WorkItem.Type.values}."})
    if priority not in WorkItem.Priority.values:
        raise ValidationError({"priority": f"Must be one of {WorkItem.Priority.values}."})

    now = timezone.now()
    item = WorkItem(
        team=team, title=title, type=type, priority=priority,
        description=description or "", due_date=due_date,
        created_by=user, created_at=now, last_activity_at=now,
    )
    item.sla_due_at = item.compute_sla_due_at()
    item.save()

    _log(item, user, E.CREATED, new=title, at=now)
    return item


@transaction.atomic
def add_comment(user, item, body):
    check(user, "comment", item)

    body = (body or "").strip()
    if not body:
        raise ValidationError({"body": "Comment cannot be empty."})

    comment = Comment.objects.create(item=item, author=user, body=body)
    # Comments don't change the item, so they don't bump its version
    # (otherwise a comment would make everyone else's open edit "stale").
    _log(item, user, E.COMMENTED, new=body, at=comment.created_at)
    return comment
