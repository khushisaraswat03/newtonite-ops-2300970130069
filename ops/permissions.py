from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404

from .models import Membership, WorkItem

VIEWER, MEMBER, LEAD, ADMIN = "viewer", "member", "lead", "admin"

WORK = {MEMBER, LEAD}
ACTION_ROLES = {
    "view": {VIEWER, MEMBER, LEAD},
    "comment": WORK,
    "claim": WORK,
    "release": WORK,
    "edit": {LEAD},
    "unassign": {LEAD},
    "handoff": {LEAD},
}


def team_roles(user):
    if not hasattr(user, "_team_roles"):
        user._team_roles = dict(Membership.objects.filter(user=user).values_list("team_id", "role"))
    return user._team_roles


def role_in_team(user, team_id):
    if user.is_superuser:
        return ADMIN
    return team_roles(user).get(team_id)


def can(user, action, item):
    role = role_in_team(user, item.team_id)
    if role == ADMIN:
        return True
    if role in ACTION_ROLES[action]:
        return True
    if action == "edit" and role in WORK and item.assignee_id == user.id:
        return True
    if item.created_by_id == user.id and action in ("view", "comment"):
        return True
    return False


def check(user, action, item):
    if not can(user, "view", item):
        raise Http404("Item not found.")
    if not can(user, action, item):
        raise PermissionDenied(f"You are not allowed to {action} this item.")


def can_raise(user):
    return user.is_superuser or any(r in WORK for r in team_roles(user).values())


def visible_items(user):
    if user.is_superuser:
        return WorkItem.objects.all()
    return WorkItem.objects.filter(Q(team_id__in=team_roles(user).keys()) | Q(created_by=user))
