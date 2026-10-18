from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import ActivityEvent, Comment, WorkItem
from .outbox import lead_ids, notify
from .permissions import ADMIN, LEAD, can_raise, check, role_in_team
from .workflow import TransitionError, check_transition

E = ActivityEvent.Type
S = WorkItem.Status


class ConflictError(Exception):
    def __init__(self, message, current=None):
        super().__init__(message)
        self.current = current


def _log(item, actor, type, old="", new="", reason="", at=None):
    at = at or timezone.now()
    ActivityEvent.objects.create(
        item=item, actor=actor, type=type,
        old_value=str(old)[:200], new_value=str(new)[:200], reason=reason, created_at=at,
    )
    WorkItem.objects.filter(pk=item.pk).update(last_activity_at=at)
    item.last_activity_at = at


def _ensure_current(item, version):
    item.refresh_from_db()
    if item.version != version:
        raise ConflictError(
            f"This item was changed by someone else (you had version {version}, "
            f"it is now version {item.version}). Reload and try again.",
            current=item,
        )



def _check_due_date(due_date):
    if due_date and due_date < timezone.localdate():
        raise ValidationError({"due_date": "The due date can't be in the past."})


@transaction.atomic
def create_item(user, *, team, title, type, priority, description="", due_date=None):
    if not can_raise(user):
        raise PermissionDenied("Viewers cannot raise items.")

    _check_due_date(due_date)
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
    _log(item, user, E.COMMENTED, new=body, at=comment.created_at)
    return comment


@transaction.atomic
def claim_item(user, item):
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
    notify([item.created_by_id], item, f"{user.username} picked up your item “{item.title}”", exclude=user.id)
    item.refresh_from_db()
    return item


@transaction.atomic
def release_item(user, item):
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
    check(user, "unassign", item)
    if not (reason or "").strip():
        raise ValidationError({"reason": "A reason is required to unassign someone."})

    previous = item.assignee
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
PRIORITY_RANK = {p: i for i, p in enumerate(WorkItem.Priority.values)}


@transaction.atomic
def update_item(user, item, *, version, priority=None, due_date=None, reason=""):
    check(user, "edit", item)
    _ensure_current(item, version)
    reason = (reason or "").strip()

    if item.status in FINISHED:
        raise ValidationError(f"{item.get_status_display()} items cannot be edited. Reopen it first.")

    changes, events = {}, []

    if priority is not None and priority != item.priority:
        if priority not in WorkItem.Priority.values:
            raise ValidationError({"priority": f"Must be one of {WorkItem.Priority.values}."})
        if role_in_team(user, item.team_id) not in (LEAD, ADMIN):
            raise PermissionDenied("Only a Lead of this team can change the priority.")
        if PRIORITY_RANK[priority] > PRIORITY_RANK[item.priority] and not reason:
            raise ValidationError({"reason": "A reason is required to lower the priority."})
        changes["priority"] = priority
        changes["sla_due_at"] = item.compute_sla_due_at(priority)
        events.append((E.PRIORITY_CHANGED, item.priority, priority))

    if due_date is not None and due_date != item.due_date:
        _check_due_date(due_date)
        if item.due_date and due_date > item.due_date and not reason:
            raise ValidationError({"reason": "A reason is required to move the due date later."})
        changes["due_date"] = due_date
        events.append((E.EDITED, f"due {item.due_date or 'none'}", f"due {due_date}"))

    if not changes:
        return item

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
    check(user, "view", item)
    _ensure_current(item, version)
    check_transition(user, item, new_status, reason)
    reason = (reason or "").strip()
    old_status = item.status
    now = timezone.now()

    changes = {"status": new_status}
    if new_status == S.RESOLVED:
        changes["resolved_at"] = now
    if old_status in (S.RESOLVED, S.CLOSED):
        changes["resolved_at"] = None
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
    _notify_transition(user, item, old_status, new_status)
    item.refresh_from_db()
    return item


def _notify_transition(user, item, old_status, new_status):
    if new_status == S.AWAITING_APPROVAL:
        notify(lead_ids(item.team_id), item, f"“{item.title}” is waiting for your approval",
               exclude=item.assignee_id)
    elif old_status == S.AWAITING_APPROVAL and new_status == S.IN_PROGRESS:
        notify([item.assignee_id], item, f"{user.username} sent “{item.title}” back to you", exclude=user.id)
    elif new_status == S.RESOLVED:
        notify([item.created_by_id], item, f"Good news: your item “{item.title}” was resolved", exclude=user.id)


HANDOFF_FROM = {S.OPEN, S.IN_PROGRESS}


@transaction.atomic
def handoff_item(user, item, new_team, *, version, reason):
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
    notify(lead_ids(old_team.pk), item, f"“{item.title}” was handed off to {new_team.name}: {reason}", exclude=user.id)
    if old_assignee:
        notify([old_assignee.pk], item, f"“{item.title}” was moved to {new_team.name}, so you're no longer on it",
               exclude=user.id)
    item.refresh_from_db()
    return item
