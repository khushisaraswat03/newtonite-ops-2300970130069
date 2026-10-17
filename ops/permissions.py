"""
Every permission decision in the app goes through this module.

Rules (decided in design):
- Admin (user.is_superuser) can do everything, in every team.
- You can SEE an item if you are in its team (any role) or you raised it.
- Anyone who is a Member or Lead somewhere can RAISE an item for any team.
- Only Members/Leads of the item's team can WORK on it.
- Only Leads of the item's team can do the Lead-only actions.
- The person who raised an item from outside the team can still comment on it.
"""

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404

from .models import Membership, WorkItem

VIEWER, MEMBER, LEAD, ADMIN = "viewer", "member", "lead", "admin"

# Actions on an existing item, and the team roles that may perform them.
WORK = {MEMBER, LEAD}
ACTION_ROLES = {
    "view": {VIEWER, MEMBER, LEAD},
    "comment": WORK,
    "claim": WORK,
    "release": WORK,
    "edit": WORK,
    "transition": WORK,  # which transitions exactly is decided by the workflow rules
    "unassign": {LEAD},
    "handoff": {LEAD},
}


def team_roles(user):
    """{team_id: role} for this user. Loaded once and cached on the user object for the request."""
    if not hasattr(user, "_team_roles"):
        user._team_roles = dict(Membership.objects.filter(user=user).values_list("team_id", "role"))
    return user._team_roles


def role_in_team(user, team_id):
    if user.is_superuser:
        return ADMIN
    return team_roles(user).get(team_id)


def can(user, action, item):
    """True if `user` may perform `action` on `item`."""
    role = role_in_team(user, item.team_id)
    if role == ADMIN:
        return True
    if role in ACTION_ROLES[action]:
        return True
    # Raised it from outside the team: may follow and comment, nothing else.
    if item.created_by_id == user.id and action in ("view", "comment"):
        return True
    return False


def check(user, action, item):
    """
    Raise if not allowed.
    404 if the user can't even see the item (don't reveal that it exists),
    403 if they can see it but may not do this action.
    """
    if not can(user, "view", item):
        raise Http404("Item not found.")
    if not can(user, action, item):
        raise PermissionDenied(f"You are not allowed to {action} this item.")


def can_raise(user):
    """Raising work is open to anyone who works somewhere (not Viewer-only users)."""
    return user.is_superuser or any(r in WORK for r in team_roles(user).values())


def visible_items(user):
    """
    Base queryset for every list and lookup, so filtering happens in the
    database and a user can never page through items they aren't allowed to see.
    """
    if user.is_superuser:
        return WorkItem.objects.all()
    return WorkItem.objects.filter(Q(team_id__in=team_roles(user).keys()) | Q(created_by=user))
