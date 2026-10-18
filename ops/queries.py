import base64
import json
from datetime import timedelta

from django.contrib.postgres.search import SearchQuery, SearchVector
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import WorkItem
from .permissions import LEAD, team_roles, visible_items

S = WorkItem.Status
ACTIVE = [S.OPEN, S.IN_PROGRESS, S.AWAITING_APPROVAL]
URGENT = [WorkItem.Priority.P1, WorkItem.Priority.P2]
STALE_AFTER = timedelta(days=3)

ORDERING = ("priority", "sla_due_at", "id")
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class BadQuery(Exception):
    pass


def lead_team_ids(user):
    return [team_id for team_id, role in team_roles(user).items() if role == LEAD]


def apply_view(qs, user, view):
    now = timezone.now()
    if view == "assigned_to_me":
        return qs.filter(assignee=user, status__in=ACTIVE)
    if view == "unassigned_urgent":
        return qs.filter(status=S.OPEN, assignee__isnull=True, priority__in=URGENT)
    if view == "awaiting_my_approval":
        qs = qs.filter(status=S.AWAITING_APPROVAL).exclude(assignee=user)
        return qs if user.is_superuser else qs.filter(team_id__in=lead_team_ids(user))
    if view == "sla_breached":
        return qs.filter(status__in=ACTIVE, sla_due_at__lt=now)
    if view == "stale":
        return qs.filter(status__in=ACTIVE, last_activity_at__lt=now - STALE_AFTER)
    if view == "raised_by_me":
        return qs.filter(created_by=user)
    raise BadQuery(f"Unknown view '{view}'.")


def _choices(value, allowed, name):
    values = [v for v in value.split(",") if v]
    bad = set(values) - set(allowed)
    if bad:
        raise BadQuery(f"Invalid {name}: {', '.join(sorted(bad))}.")
    return values


def filter_items(user, params):
    qs = visible_items(user).select_related("team", "assignee", "created_by")

    if params.get("view"):
        qs = apply_view(qs, user, params["view"])
    if params.get("status"):
        qs = qs.filter(status__in=_choices(params["status"], S.values, "status"))
    if params.get("priority"):
        qs = qs.filter(priority__in=_choices(params["priority"], WorkItem.Priority.values, "priority"))
    if params.get("type"):
        qs = qs.filter(type__in=_choices(params["type"], WorkItem.Type.values, "type"))
    if params.get("team"):
        if not params["team"].isdigit():
            raise BadQuery("team must be a team id.")
        qs = qs.filter(team_id=int(params["team"]))

    assignee = params.get("assignee")
    if assignee == "me":
        qs = qs.filter(assignee=user)
    elif assignee == "none":
        qs = qs.filter(assignee__isnull=True)
    elif assignee:
        if not assignee.isdigit():
            raise BadQuery("assignee must be a user id, 'me' or 'none'.")
        qs = qs.filter(assignee_id=int(assignee))

    if params.get("q"):
        qs = qs.annotate(search=SearchVector("title", "description", config="english")).filter(
            search=SearchQuery(params["q"], config="english", search_type="websearch")
        )

    return qs.order_by(*ORDERING)


def _encode_cursor(item):
    raw = json.dumps([item.priority, item.sla_due_at.isoformat(), item.id])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor):
    try:
        priority, sla, item_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        sla = parse_datetime(sla)
        if sla is None or not isinstance(item_id, int):
            raise ValueError
        return priority, sla, item_id
    except (ValueError, TypeError):
        raise BadQuery("Invalid cursor.")


def paginate(qs, cursor=None, page_size=None):
    try:
        size = min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
    except ValueError:
        raise BadQuery("page_size must be a number.")
    if size < 1:
        raise BadQuery("page_size must be at least 1.")

    if cursor:
        p, sla, item_id = _decode_cursor(cursor)
        qs = qs.filter(
            Q(priority__gt=p)
            | Q(priority=p, sla_due_at__gt=sla)
            | Q(priority=p, sla_due_at=sla, id__gt=item_id)
        )

    rows = list(qs[: size + 1])
    items = rows[:size]
    next_cursor = _encode_cursor(items[-1]) if len(rows) > size else None
    return items, next_cursor
