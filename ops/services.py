"""
All writes to work items go through these functions.

Each one runs in a single database transaction that saves the change AND its
activity event together, so the history can never disagree with the item.
Views (API and HTML) only parse input and call these.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import ActivityEvent, Comment, WorkItem
from .permissions import can_raise, check

E = ActivityEvent.Type
S = WorkItem.Status


class ConflictError(Exception):
    """Someone else changed the item first (lost a race, or a stale version). Maps to HTTP 409."""


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


@transaction.atomic
def claim_item(user, item):
    """
    Take an Open, unassigned item and start work on it.

    The check and the change are ONE conditional UPDATE, so two people clicking
    at the same moment can't both win: Postgres locks the row for the first
    UPDATE, the second waits, then re-checks "assignee IS NULL", finds it false,
    and updates 0 rows.
    """
    check(user, "claim", item)

    updated = WorkItem.objects.filter(
        pk=item.pk, status=S.OPEN, assignee__isnull=True,
    ).update(
        assignee=user, status=S.IN_PROGRESS, version=F("version") + 1, updated_at=timezone.now(),
    )
    if updated == 0:
        raise ConflictError("Someone else has already claimed this item.")

    _log(item, user, E.CLAIMED, new=user.username)
    _log(item, user, E.STATUS_CHANGED, old=S.OPEN, new=S.IN_PROGRESS)
    item.refresh_from_db()
    return item


@transaction.atomic
def release_item(user, item):
    """The assignee gives an In Progress item back to the team queue."""
    check(user, "release", item)

    updated = WorkItem.objects.filter(
        pk=item.pk, status=S.IN_PROGRESS, assignee=user,
    ).update(
        assignee=None, status=S.OPEN, version=F("version") + 1, updated_at=timezone.now(),
    )
    if updated == 0:
        raise ConflictError("You can only release an In Progress item that is assigned to you.")

    _log(item, user, E.RELEASED, old=user.username)
    _log(item, user, E.STATUS_CHANGED, old=S.IN_PROGRESS, new=S.OPEN)
    item.refresh_from_db()
    return item


@transaction.atomic
def unassign_item(user, item, reason):
    """
    A Lead frees up a stuck item so someone else can claim it.
    Leads never choose who does the work (pull model); they only return it to the queue.
    """
    check(user, "unassign", item)
    if not (reason or "").strip():
        raise ValidationError({"reason": "A reason is required to unassign someone."})

    previous = item.assignee
    # Only unassign the person the Lead saw: if it changed meanwhile, don't touch it.
    updated = WorkItem.objects.filter(
        pk=item.pk, status=S.IN_PROGRESS, assignee=previous, assignee__isnull=False,
    ).update(
        assignee=None, status=S.OPEN, version=F("version") + 1, updated_at=timezone.now(),
    )
    if updated == 0:
        raise ConflictError("This item is no longer assigned to that person. Reload and try again.")

    _log(item, user, E.UNASSIGNED, old=previous.username, reason=reason.strip())
    _log(item, user, E.STATUS_CHANGED, old=S.IN_PROGRESS, new=S.OPEN)
    item.refresh_from_db()
    return item
