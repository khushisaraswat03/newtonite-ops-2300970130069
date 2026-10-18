import threading

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.http import Http404

from ops.models import ActivityEvent, WorkItem
from ops.services import ConflictError, claim_item, release_item, unassign_item

S = WorkItem.Status


# transaction=True: each thread needs its own real, committed transaction,
# otherwise the test can't reproduce two simultaneous database requests.
@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_only_one_wins(world, open_item):
    claimers = [world.alice, world.bob, world.lead]
    barrier = threading.Barrier(len(claimers))  # release all threads at the same instant
    results = {}

    def attempt(user):
        try:
            item = WorkItem.objects.get(pk=open_item.pk)  # each "browser" loaded the item as Open
            barrier.wait()
            claim_item(user, item)
            results[user.username] = "won"
        except ConflictError:
            results[user.username] = "conflict"
        finally:
            connection.close()  # each thread opened its own DB connection

    threads = [threading.Thread(target=attempt, args=(u,)) for u in claimers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["conflict", "conflict", "won"]
    winner = next(name for name, r in results.items() if r == "won")

    item = WorkItem.objects.get(pk=open_item.pk)
    assert item.assignee.username == winner
    assert item.status == S.IN_PROGRESS
    assert item.version == 2
    # History agrees: exactly one claim recorded, by the winner.
    claims = ActivityEvent.objects.filter(item=item, type="claimed")
    assert [e.actor.username for e in claims] == [winner]


def test_claim_already_claimed_item_conflicts(world, open_item):
    claim_item(world.alice, open_item)
    with pytest.raises(ConflictError):
        claim_item(world.bob, open_item)


def test_viewer_and_outsider_cannot_claim(world, open_item):
    with pytest.raises(PermissionDenied):
        claim_item(world.viewer, open_item)
    with pytest.raises(Http404):
        claim_item(world.outsider, open_item)


def test_release_returns_item_to_queue(world, open_item):
    claim_item(world.alice, open_item)
    item = release_item(world.alice, open_item)
    assert item.status == S.OPEN
    assert item.assignee is None
    # And now someone else can pick it up.
    claim_item(world.bob, item)


def test_only_assignee_can_release(world, open_item):
    claim_item(world.alice, open_item)
    with pytest.raises(ConflictError):
        release_item(world.bob, open_item)


def test_lead_unassign_requires_reason_and_is_logged(world, open_item):
    item = claim_item(world.alice, open_item)
    with pytest.raises(ValidationError):
        unassign_item(world.lead, item, reason="  ")

    item = unassign_item(world.lead, item, reason="Alice is on leave")
    assert item.status == S.OPEN and item.assignee is None
    event = ActivityEvent.objects.get(item=item, type="unassigned")
    assert event.old_value == "alice" and event.reason == "Alice is on leave"


def test_member_cannot_unassign(world, open_item):
    item = claim_item(world.alice, open_item)
    with pytest.raises(PermissionDenied):
        unassign_item(world.bob, item, reason="I want it")
