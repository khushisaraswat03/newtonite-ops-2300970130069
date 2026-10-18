from django.core.exceptions import PermissionDenied
from django.http import Http404

from .models import WorkItem
from .permissions import ADMIN, LEAD, MEMBER, can, check, role_in_team

S = WorkItem.Status

TRANSITIONS = {
    (S.IN_PROGRESS, S.AWAITING_APPROVAL): {"who": "assignee", "reason": False},
    (S.AWAITING_APPROVAL, S.RESOLVED): {"who": "approver", "reason": False},
    (S.AWAITING_APPROVAL, S.IN_PROGRESS): {"who": "lead", "reason": True},
    (S.RESOLVED, S.CLOSED): {"who": "lead", "reason": False},
    (S.RESOLVED, S.IN_PROGRESS): {"who": "assignee", "reason": True},
    (S.CLOSED, S.IN_PROGRESS): {"who": "assignee", "reason": True},
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
    pass


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
    return {
        "claim": can(user, "claim", item) and item.status == S.OPEN and item.assignee_id is None,
        "release": item.assignee_id == user.id and item.status == S.IN_PROGRESS,
        "unassign": can(user, "unassign", item) and item.assignee_id is not None and item.status == S.IN_PROGRESS,
        "edit": can(user, "edit", item) and item.status not in (S.CLOSED, S.CANCELLED),
        "handoff": can(user, "handoff", item) and item.status in (S.OPEN, S.IN_PROGRESS),
        "comment": can(user, "comment", item),
        "transitions": allowed_transitions(user, item),
    }
