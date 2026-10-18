from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from ops.models import ActivityEvent, WorkItem
from ops.services import ConflictError, claim_item, update_item


@pytest.fixture
def claimed_item(world, open_item):
    return claim_item(world.alice, open_item)


def test_stale_update_is_rejected_and_data_unchanged(world, claimed_item):
    alice_view = WorkItem.objects.get(pk=claimed_item.pk)
    lead_view = WorkItem.objects.get(pk=claimed_item.pk)

    update_item(world.alice, alice_view, version=2, priority="P1")

    with pytest.raises(ConflictError) as exc:
        update_item(world.lead, lead_view, version=2, due_date=date(2026, 12, 1))

    assert exc.value.current.version == 3
    item = WorkItem.objects.get(pk=claimed_item.pk)
    assert item.priority == "P1"
    assert item.due_date is None
    assert item.version == 3


def test_assignee_can_raise_priority_and_sla_is_recalculated(world, claimed_item):
    item = update_item(world.alice, claimed_item, version=claimed_item.version, priority="P1")
    assert item.priority == "P1"
    assert item.sla_due_at == item.team_since + WorkItem.SLA["P1"]
    event = ActivityEvent.objects.get(item=item, type="priority_changed")
    assert (event.old_value, event.new_value) == ("P2", "P1")


def test_only_lead_can_lower_priority_and_needs_reason(world, claimed_item):
    with pytest.raises(PermissionDenied):
        update_item(world.alice, claimed_item, version=claimed_item.version, priority="P4")
    with pytest.raises(ValidationError):
        update_item(world.lead, claimed_item, version=claimed_item.version, priority="P4")

    item = update_item(
        world.lead, claimed_item, version=claimed_item.version, priority="P4",
        reason="One customer affected, workaround exists",
    )
    assert item.priority == "P4"
    assert ActivityEvent.objects.get(item=item, type="priority_changed").reason


def test_team_member_who_is_not_assignee_cannot_edit(world, claimed_item):
    with pytest.raises(PermissionDenied):
        update_item(world.bob, claimed_item, version=claimed_item.version, priority="P1")


def test_moving_due_date_later_needs_reason(world, claimed_item):
    item = update_item(world.alice, claimed_item, version=claimed_item.version, due_date=date(2026, 11, 1))
    with pytest.raises(ValidationError):
        update_item(world.alice, item, version=item.version, due_date=date(2026, 11, 5))
    item = update_item(
        world.alice, item, version=item.version, due_date=date(2026, 11, 5),
        reason="Waiting on bank confirmation",
    )
    assert item.due_date == date(2026, 11, 5)


def test_finished_items_cannot_be_edited(world, claimed_item):
    WorkItem.objects.filter(pk=claimed_item.pk).update(status="closed")
    claimed_item.refresh_from_db()
    with pytest.raises(ValidationError):
        update_item(world.lead, claimed_item, version=claimed_item.version, priority="P1")


def test_no_change_does_not_bump_version(world, claimed_item):
    item = update_item(world.alice, claimed_item, version=claimed_item.version, priority="P2")
    assert item.version == claimed_item.version


def test_stale_request_gets_409_even_if_it_also_breaks_a_rule(world, claimed_item):
    lead_view = WorkItem.objects.get(pk=claimed_item.pk)
    update_item(world.alice, claimed_item, version=2, priority="P1")
    with pytest.raises(ConflictError):
        update_item(world.lead, lead_view, version=2, priority="P4")
