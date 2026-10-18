"""
Workflow rules: which status changes are allowed, who may make them, and when a reason is required.

    Open ─claim─▶ In Progress ──▶ Awaiting Approval ──approve──▶ Resolved ──▶ Closed
     ▲               ▲                  │                          │           │
     │               └──── reject ──────┘                          │           │
     │               └──── reopen by previous assignee (keeps it) ─┴───────────┤
     └──────────────────── reopen by Lead or creator (back to queue) ──────────┘

    Open / In Progress / Awaiting Approval ──cancel──▶ Cancelled (final)

Open -> In Progress is not a transition here: it only happens by claiming
(see services.claim_item), and release/unassign send an item back to Open.
"""

from django.core.exceptions import PermissionDenied
from django.http import Http404

from .models import WorkItem
from .permissions import ADMIN, LEAD, MEMBER, can, check, role_in_team

S = WorkItem.Status

# Who may make a transition:
#   "assignee"        - the person who claimed the item (must still be in the team)
#   "lead"            - a Lead of the item's team
#   "approver"        - a Lead of the item's team who is NOT the assignee (four-eyes)
#   "lead_or_creator" - a Lead, or whoever raised the item (even from another team)
# Admins count as Leads everywhere.
TRANSITIONS = {
    (S.IN_PROGRESS, S.AWAITING_APPROVAL): {"who": "assignee", "reason": False},
    (S.AWAITING_APPROVAL, S.RESOLVED): {"who": "approver", "reason": False},
    (S.AWAITING_APPROVAL, S.IN_PROGRESS): {"who": "lead", "reason": True},  # reject
    (S.RESOLVED, S.CLOSED): {"who": "lead", "reason": False},
    # Reopen: the previous assignee continues the work themselves...
    (S.RESOLVED, S.IN_PROGRESS): {"who": "assignee", "reason": True},
    (S.CLOSED, S.IN_PROGRESS): {"who": "assignee", "reason": True},
    # ...or a Lead / the creator sends it back to the team queue for anyone to claim.
    (S.RESOLVED, S.OPEN): {"who": "lead_or_creator", "reason": True},
    (S.CLOSED, S.OPEN): {"who": "lead_or_creator", "reason": True},
    (S.OPEN, S.CANCELLED): {"who": "lead", "reason": True},
    (S.IN_PROGRESS, S.CANCELLED): {"who": "lead", "reason": True},
    (S.AWAITING_APPROVAL, S.CANCELLED): {"who": "lead", "reason": True},
}

DENIED_MESSAGES = {
    "assignee": "Only the assignee can make this change.",
    "lead": "Only a Lead of this team can make this change.",
    "approver": "Only a Lead of this team can approve.",
    "lead_or_creator": "Only a Lead of this team or the person who raised the item can do this.",
}


class TransitionError(Exception):
    """The requested status change is not valid (bad jump, missing reason)."""


def _who_allowed(user, item, who):
    role = role_in_team(user, item.team_id)
    is_lead = role in (LEAD, ADMIN)
    if who == "assignee":
        return role == ADMIN or (item.assignee_id == user.id and role in (MEMBER, LEAD))
    if who == "lead":
        return is_lead
    if who == "approver":
        return is_lead and item.assignee_id != user.id
    if who == "lead_or_creator":
        return is_lead or item.created_by_id == user.id
    raise ValueError(who)


def check_transition(user, item, new_status, reason=""):
    """Raise TransitionError or PermissionDenied unless `user` may move `item` to `new_status`."""
    # 404 if they can't see it at all; beyond that, the "who" rule below decides
    # (this is what lets a creator from another team reopen their own item).
    check(user, "view", item)

    rule = TRANSITIONS.get((item.status, new_status))
    if rule is None:
        raise TransitionError(f"Cannot move an item from {item.status} to {new_status}.")

    if not _who_allowed(user, item, rule["who"]):
        if rule["who"] == "approver" and item.assignee_id == user.id:
            raise PermissionDenied("You cannot approve your own work. Another Lead must approve it.")
        raise PermissionDenied(DENIED_MESSAGES[rule["who"]])

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


def allowed_actions(user, item):
    """Everything the user may do with the item right now, so screens show only valid buttons.
    The server still checks every action; this is a convenience, not security."""
    return {
        "claim": can(user, "claim", item) and item.status == S.OPEN and item.assignee_id is None,
        "release": item.assignee_id == user.id and item.status == S.IN_PROGRESS,
        "unassign": can(user, "unassign", item) and item.assignee_id is not None and item.status == S.IN_PROGRESS,
        "edit": can(user, "edit", item) and item.status not in (S.CLOSED, S.CANCELLED),
        "handoff": can(user, "handoff", item) and item.status in (S.OPEN, S.IN_PROGRESS),
        "comment": can(user, "comment", item),
        "transitions": allowed_transitions(user, item),
    }
