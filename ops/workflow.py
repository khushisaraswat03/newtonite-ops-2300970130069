"""
Workflow rules: which status changes are allowed, who may make them, and when a reason is required.

    Open ─claim─▶ In Progress ──▶ Awaiting Approval ──approve──▶ Resolved ──▶ Closed
                 ▲                  │                          │           │
                 └──── reject ──────┘                          │           │
                 └──────────────────── reopen ─────────────────┴───────────┘

    Open / In Progress / Awaiting Approval ──cancel──▶ Cancelled (final)

Open -> In Progress is not a transition here: it only happens by claiming
(see services.claim_item), and release/unassign send an item back to Open.
"""

from django.core.exceptions import PermissionDenied
from django.http import Http404

from .models import WorkItem
from .permissions import ADMIN, LEAD, check, role_in_team

S = WorkItem.Status

# Who may make a transition:
#   "assignee" - the person who claimed the item
#   "lead"     - a Lead of the item's team
#   "approver" - a Lead of the item's team who is NOT the assignee (four-eyes)
# Admins count as Leads everywhere.
TRANSITIONS = {
    (S.IN_PROGRESS, S.AWAITING_APPROVAL): {"who": "assignee", "reason": False},
    (S.AWAITING_APPROVAL, S.RESOLVED): {"who": "approver", "reason": False},
    (S.AWAITING_APPROVAL, S.IN_PROGRESS): {"who": "lead", "reason": True},  # reject
    (S.RESOLVED, S.CLOSED): {"who": "lead", "reason": False},
    (S.RESOLVED, S.IN_PROGRESS): {"who": "lead", "reason": True},  # reopen
    (S.CLOSED, S.IN_PROGRESS): {"who": "lead", "reason": True},  # reopen
    (S.OPEN, S.CANCELLED): {"who": "lead", "reason": True},
    (S.IN_PROGRESS, S.CANCELLED): {"who": "lead", "reason": True},
    (S.AWAITING_APPROVAL, S.CANCELLED): {"who": "lead", "reason": True},
}


class TransitionError(Exception):
    """The requested status change is not valid (bad jump, missing reason, unclaimed item)."""


def _is_lead(user, item):
    return role_in_team(user, item.team_id) in (LEAD, ADMIN)


def _who_allowed(user, item, who):
    if who == "assignee":
        return item.assignee_id == user.id or user.is_superuser
    if who == "lead":
        return _is_lead(user, item)
    if who == "approver":
        return _is_lead(user, item) and item.assignee_id != user.id
    raise ValueError(who)


def check_transition(user, item, new_status, reason=""):
    """Raise TransitionError or PermissionDenied unless `user` may move `item` to `new_status`."""
    check(user, "transition", item)

    rule = TRANSITIONS.get((item.status, new_status))
    if rule is None:
        raise TransitionError(f"Cannot move an item from {item.status} to {new_status}.")

    if not _who_allowed(user, item, rule["who"]):
        if rule["who"] == "approver" and item.assignee_id == user.id:
            raise PermissionDenied("You cannot approve your own work. Another Lead must approve it.")
        if rule["who"] == "assignee":
            raise PermissionDenied("Only the assignee can make this change.")
        raise PermissionDenied("Only a Lead of this team can make this change.")

    if rule["reason"] and not reason.strip():
        raise TransitionError("A reason is required for this change.")


def allowed_transitions(user, item):
    """Statuses this user could move the item to right now (used to show the right buttons)."""
    result = []
    for (old, new) in TRANSITIONS:
        if old != item.status:
            continue
        try:
            check_transition(user, item, new, reason="(check)")
        except (TransitionError, PermissionDenied, Http404):
            continue
        result.append(new)
    return result
