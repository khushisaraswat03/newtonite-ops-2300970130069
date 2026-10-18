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
from .permissions import ADMIN, LEAD, can_raise, check, role_in_team
from .workflow import TransitionError, check_transition

E = ActivityEvent.Type
S = WorkItem.Status


class ConflictError(Exception):
    """Someone else changed the item first (lost a race, or a stale version). Maps to HTTP 409."""

    def __init__(self, message, current=None):
        super().__init__(message)
        self.current = current  # the latest item, so the client can show what changed


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


def _ensure_current(item, version):
    """
    Reject a stale request before checking anything else: if the user was looking at an
    old version, "you need a reason" or "invalid transition" would be about data they
    haven't seen. (The conditional UPDATE still guards the race between here and the write.)
    """
    item.refresh_from_db()  # judge the request against the latest data, not the caller's copy
    if item.version != version:
        raise ConflictError(
            f"This item was changed by someone else (you had version {version}, "
            f"it is now version {item.version}). Reload and try again.",
            current=item,
        )


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
        created_by=user, created_at=now, team_since=now, last_activity_at=now,
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
        raise ConflictError("Someone else has already claimed this item.", current=WorkItem.objects.get(pk=item.pk))

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
        raise ConflictError("You can only release an In Progress item that is assigned to you.", current=WorkItem.objects.get(pk=item.pk))

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
        raise ConflictError("This item is no longer assigned to that person. Reload and try again.", current=WorkItem.objects.get(pk=item.pk))

    _log(item, user, E.UNASSIGNED, old=previous.username, reason=reason.strip())
    _log(item, user, E.STATUS_CHANGED, old=S.IN_PROGRESS, new=S.OPEN)
    item.refresh_from_db()
    return item


FINISHED = {S.CLOSED, S.CANCELLED}
# P1 is the most urgent: a higher number means lower priority.
PRIORITY_RANK = {p: i for i, p in enumerate(WorkItem.Priority.values)}


@transaction.atomic
def update_item(user, item, *, version, priority=None, due_date=None, reason=""):
    """
    Change priority and/or due date. Title and description are the original
    report and are not editable; clarifications go in comments.

    Optimistic locking: `version` is the version the user was looking at.
    The UPDATE only applies WHERE version = <that version>; if someone changed
    the item in the meantime, 0 rows match and we return 409 with the latest item.
    """
    check(user, "edit", item)
    _ensure_current(item, version)
    reason = (reason or "").strip()

    if item.status in FINISHED:
        raise ValidationError(f"{item.get_status_display()} items cannot be edited. Reopen it first.")

    changes, events = {}, []

    if priority is not None and priority != item.priority:
        if priority not in WorkItem.Priority.values:
            raise ValidationError({"priority": f"Must be one of {WorkItem.Priority.values}."})
        if PRIORITY_RANK[priority] > PRIORITY_RANK[item.priority]:  # lowering
            if role_in_team(user, item.team_id) not in (LEAD, ADMIN):
                raise PermissionDenied("Only a Lead can lower the priority.")
            if not reason:
                raise ValidationError({"reason": "A reason is required to lower the priority."})
        changes["priority"] = priority
        changes["sla_due_at"] = item.compute_sla_due_at(priority)
        events.append((E.PRIORITY_CHANGED, item.priority, priority))

    if due_date is not None and due_date != item.due_date:
        if item.due_date and due_date > item.due_date and not reason:
            raise ValidationError({"reason": "A reason is required to move the due date later."})
        changes["due_date"] = due_date
        events.append((E.EDITED, f"due {item.due_date or 'none'}", f"due {due_date}"))

    if not changes:
        return item  # nothing changed: no new version, no history noise

    updated = WorkItem.objects.filter(pk=item.pk, version=version).update(
        **changes, version=F("version") + 1, updated_at=timezone.now(),
    )
    if updated == 0:
        current = WorkItem.objects.get(pk=item.pk)
        raise ConflictError(
            f"This item was changed by someone else (you had version {version}, "
            f"it is now version {current.version}). Reload and try again.",
            current=current,
        )

    for type, old, new in events:
        _log(item, user, type, old=old, new=new, reason=reason)
    item.refresh_from_db()
    return item


@transaction.atomic
def transition_item(user, item, new_status, *, version, reason=""):
    """
    Move an item to a new status, following the rules in workflow.py.
    Uses the same optimistic lock as update_item: the change only applies if
    the item is still at the version (and status) the user was looking at.
    """
    check(user, "view", item)
    _ensure_current(item, version)
    check_transition(user, item, new_status, reason)
    reason = (reason or "").strip()
    old_status = item.status
    now = timezone.now()

    changes = {"status": new_status}
    if new_status == S.RESOLVED:
        changes["resolved_at"] = now  # stops the SLA clock
    if old_status in (S.RESOLVED, S.CLOSED):
        changes["resolved_at"] = None  # reopened: it is not resolved any more
    # Reopened to the queue by a Lead or the creator: anyone can claim it again.
    clears_assignee = new_status == S.OPEN and item.assignee_id is not None
    if clears_assignee:
        changes["assignee"] = None

    updated = WorkItem.objects.filter(pk=item.pk, version=version, status=old_status).update(
        **changes, version=F("version") + 1, updated_at=now,
    )
    if updated == 0:
        current = WorkItem.objects.get(pk=item.pk)
        raise ConflictError(
            "This item was changed by someone else. Reload and try again.", current=current,
        )

    _log(item, user, E.STATUS_CHANGED, old=old_status, new=new_status, reason=reason, at=now)
    if clears_assignee:
        _log(item, user, E.UNASSIGNED, old=item.assignee.username, reason="Reopened to the team queue", at=now)
    item.refresh_from_db()
    return item


HANDOFF_FROM = {S.OPEN, S.IN_PROGRESS}


@transaction.atomic
def handoff_item(user, item, new_team, *, version, reason):
    """
    A Lead moves an item to the team that should really handle it.

    In one step it lands in the new team's queue: status Open, no assignee
    (the old assignee isn't in the new team). Priority is kept so an urgent
    item doesn't get lost; the new team's Lead can lower it with a reason.
    The SLA restarts for the new team; created_at still shows the total wait.
    """
    check(user, "handoff", item)
    _ensure_current(item, version)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required to hand off an item."})
    if new_team.pk == item.team_id:
        raise ValidationError({"team": "The item is already in this team."})
    if item.status not in HANDOFF_FROM:
        raise TransitionError("Only Open or In Progress items can be handed off.")

    old_team, old_status, old_assignee = item.team, item.status, item.assignee
    now = timezone.now()

    updated = WorkItem.objects.filter(
        pk=item.pk, version=version, team=old_team, status__in=HANDOFF_FROM,
    ).update(
        team=new_team, status=S.OPEN, assignee=None,
        team_since=now, sla_due_at=now + WorkItem.SLA[item.priority],
        version=F("version") + 1, updated_at=now,
    )
    if updated == 0:
        current = WorkItem.objects.get(pk=item.pk)
        raise ConflictError("This item was changed by someone else. Reload and try again.", current=current)

    _log(item, user, E.HANDED_OFF, old=old_team.name, new=new_team.name, reason=reason, at=now)
    if old_status != S.OPEN:
        _log(item, user, E.STATUS_CHANGED, old=old_status, new=S.OPEN, reason=f"Handed off to {new_team.name}", at=now)
    if old_assignee:
        _log(item, user, E.UNASSIGNED, old=old_assignee.username, reason=f"Handed off to {new_team.name}", at=now)
    item.refresh_from_db()
    return item
