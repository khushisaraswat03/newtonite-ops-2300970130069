import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from ops.models import ActivityEvent, WorkItem
from ops.permissions import check
from ops.services import ConflictError, claim_item, handoff_item, transition_item
from ops.workflow import TransitionError

S = WorkItem.Status


@pytest.fixture
def in_progress_item(world, open_item):
    return claim_item(world.alice, open_item)


def test_lead_hands_off_in_progress_item_in_one_step(world, in_progress_item):
    before = in_progress_item
    item = handoff_item(
        world.lead, in_progress_item, world.compliance,
        version=before.version, reason="KYC expired: this is a Compliance issue",
    )

    assert item.team == world.compliance
    assert item.status == S.OPEN
    assert item.assignee is None
    assert item.priority == "P2"  # kept
    # SLA restarts for the new team; created_at is unchanged (total wait still visible).
    assert item.team_since > item.created_at
    assert item.sla_due_at == item.team_since + WorkItem.SLA["P2"]

    types = list(ActivityEvent.objects.filter(item=item).order_by("id").values_list("type", flat=True))
    assert types[-3:] == ["handed_off", "status_changed", "unassigned"]


def test_new_team_can_work_it_and_old_team_loses_access(world, in_progress_item):
    item = handoff_item(world.lead, in_progress_item, world.compliance, version=in_progress_item.version, reason="Compliance")

    claim_item(world.outsider, item)  # a Compliance member can now claim it
    with pytest.raises(Http404):
        check(world.bob, "view", item)  # Payments member no longer sees it
    check(world.alice, "view", item)  # alice raised it, so she can still follow it


def test_handoff_needs_reason(world, in_progress_item):
    with pytest.raises(ValidationError):
        handoff_item(world.lead, in_progress_item, world.compliance, version=in_progress_item.version, reason="")


def test_member_cannot_hand_off(world, in_progress_item):
    with pytest.raises(PermissionDenied):
        handoff_item(world.alice, in_progress_item, world.compliance, version=in_progress_item.version, reason="x")


def test_awaiting_approval_cannot_be_handed_off(world, in_progress_item):
    item = transition_item(world.alice, in_progress_item, S.AWAITING_APPROVAL, version=in_progress_item.version)
    with pytest.raises(TransitionError):
        handoff_item(world.lead, item, world.compliance, version=item.version, reason="x")


def test_handoff_to_same_team_rejected(world, open_item):
    with pytest.raises(ValidationError):
        handoff_item(world.lead, open_item, world.payments, version=open_item.version, reason="x")


def test_handoff_on_stale_version_conflicts(world, open_item):
    stale = open_item.version
    claim_item(world.alice, open_item)
    open_item.refresh_from_db()
    with pytest.raises(ConflictError):
        handoff_item(world.lead, open_item, world.compliance, version=stale, reason="x")
