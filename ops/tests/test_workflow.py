import pytest
from django.core.exceptions import PermissionDenied

from ops.models import ActivityEvent, WorkItem
from ops.services import ConflictError, claim_item, create_item, transition_item
from ops.workflow import TransitionError

S = WorkItem.Status


def move(user, item, status, reason=""):
    """Transition using the item's current version, like a user who just loaded the page."""
    item.refresh_from_db()
    return transition_item(user, item, status, version=item.version, reason=reason)


@pytest.fixture
def resolved_item(world, open_item):
    item = claim_item(world.alice, open_item)
    move(world.alice, item, S.AWAITING_APPROVAL)
    return move(world.lead, item, S.RESOLVED)


def test_full_happy_path_is_recorded_in_history(world, open_item):
    item = claim_item(world.alice, open_item)
    move(world.alice, item, S.AWAITING_APPROVAL)
    move(world.lead, item, S.RESOLVED)
    assert item.resolved_at is not None
    move(world.lead, item, S.CLOSED)

    changes = list(
        ActivityEvent.objects.filter(item=item, type="status_changed")
        .order_by("id").values_list("old_value", "new_value")
    )
    assert changes == [
        ("open", "in_progress"),
        ("in_progress", "awaiting_approval"),
        ("awaiting_approval", "resolved"),
        ("resolved", "closed"),
    ]


def test_invalid_jump_is_rejected(world, open_item):
    with pytest.raises(TransitionError):
        move(world.lead, open_item, S.CLOSED)
    open_item.refresh_from_db()
    assert open_item.status == S.OPEN


def test_member_cannot_approve(world, open_item):
    item = claim_item(world.alice, open_item)
    move(world.alice, item, S.AWAITING_APPROVAL)
    with pytest.raises(PermissionDenied):
        move(world.bob, item, S.RESOLVED)


def test_lead_cannot_approve_own_work_but_another_lead_can(world, open_item):
    item = claim_item(world.lead, open_item)
    move(world.lead, item, S.AWAITING_APPROVAL)
    with pytest.raises(PermissionDenied, match="own work"):
        move(world.lead, item, S.RESOLVED)
    move(world.lead2, item, S.RESOLVED)
    assert item.status == S.RESOLVED


def test_reject_needs_reason_and_keeps_assignee(world, open_item):
    item = claim_item(world.alice, open_item)
    move(world.alice, item, S.AWAITING_APPROVAL)
    with pytest.raises(TransitionError):
        move(world.lead, item, S.IN_PROGRESS)
    move(world.lead, item, S.IN_PROGRESS, reason="Missing bank reference")
    assert item.status == S.IN_PROGRESS and item.assignee == world.alice


def test_previous_assignee_reopens_and_keeps_it(world, resolved_item):
    item = move(world.alice, resolved_item, S.IN_PROGRESS, reason="Fix missed one case")
    assert item.status == S.IN_PROGRESS
    assert item.assignee == world.alice
    assert item.resolved_at is None


def test_lead_reopen_sends_item_back_to_queue(world, resolved_item):
    item = move(world.lead, resolved_item, S.OPEN, reason="Customer still affected")
    assert item.status == S.OPEN and item.assignee is None
    assert ActivityEvent.objects.filter(item=item, type="unassigned").exists()


def test_creator_from_another_team_can_reopen_to_queue(world):
    # Compliance member raises an item for Payments, Payments resolves it.
    item = create_item(world.outsider, team=world.payments, title="Wrong fee charged", type="payment", priority="P3")
    claim_item(world.alice, item)
    move(world.alice, item, S.AWAITING_APPROVAL)
    move(world.lead, item, S.RESOLVED)

    item = move(world.outsider, item, S.OPEN, reason="Fee still showing on statement")
    assert item.status == S.OPEN and item.assignee is None


def test_other_member_cannot_reopen(world, resolved_item):
    with pytest.raises(PermissionDenied):
        move(world.bob, resolved_item, S.OPEN, reason="I think it's broken")
    with pytest.raises(PermissionDenied):
        move(world.bob, resolved_item, S.IN_PROGRESS, reason="I think it's broken")


def test_cancelled_is_final(world, open_item):
    item = move(world.lead, open_item, S.CANCELLED, reason="Duplicate")
    for status in (S.OPEN, S.IN_PROGRESS):
        with pytest.raises(TransitionError):
            move(world.lead, item, status, reason="Changed my mind")


def test_viewer_cannot_change_status(world, open_item):
    with pytest.raises(PermissionDenied):
        move(world.viewer, open_item, S.CANCELLED, reason="x")


def test_transition_on_stale_version_conflicts(world, open_item):
    item = claim_item(world.alice, open_item)
    stale_version = item.version
    move(world.alice, item, S.AWAITING_APPROVAL)  # version moves on
    with pytest.raises(ConflictError):
        transition_item(world.lead, item, S.CANCELLED, version=stale_version, reason="Not needed")
