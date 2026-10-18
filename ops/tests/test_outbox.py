from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from ops import outbox
from ops.models import Membership, Notification, OutboxJob, WorkItem
from ops.services import ConflictError, claim_item, create_item, handoff_item, transition_item

S = WorkItem.Status


def make_due():
    OutboxJob.objects.filter(status="pending").update(run_after=timezone.now() - timedelta(seconds=1))


@pytest.fixture
def raised_by_outsider(world):
    return create_item(world.outsider, team=world.payments, title="Wrong fee", type="payment", priority="P3")


def test_job_is_written_with_the_change(world, raised_by_outsider):
    claim_item(world.alice, raised_by_outsider)
    job = OutboxJob.objects.get()
    assert job.kind == "notify" and job.status == "pending"
    assert job.payload["user_id"] == world.outsider.id


def test_no_job_when_the_change_fails(world, raised_by_outsider):
    claim_item(world.alice, raised_by_outsider)
    OutboxJob.objects.all().delete()
    with pytest.raises(ConflictError):
        claim_item(world.bob, raised_by_outsider)
    assert not OutboxJob.objects.exists()


def test_worker_delivers_notification(world, raised_by_outsider):
    claim_item(world.alice, raised_by_outsider)
    assert outbox.process_batch() == {"done": 1, "retry": 0, "failed": 0}

    note = Notification.objects.get()
    assert note.user == world.outsider and "alice picked up" in note.message
    assert OutboxJob.objects.get().status == "done"
    assert outbox.process_batch() == {"done": 0, "retry": 0, "failed": 0}


def test_handler_is_idempotent(world, raised_by_outsider):
    claim_item(world.alice, raised_by_outsider)
    job = OutboxJob.objects.get()
    outbox.handle_notify(job)
    outbox.handle_notify(job)
    assert Notification.objects.count() == 1


def test_failing_job_retries_with_backoff_then_fails(world, raised_by_outsider, monkeypatch):
    claim_item(world.alice, raised_by_outsider)

    def broken(job):
        raise ConnectionError("notification service down")

    monkeypatch.setitem(outbox.HANDLERS, "notify", broken)

    assert outbox.process_batch()["retry"] == 1
    job = OutboxJob.objects.get()
    assert job.status == "pending" and job.attempts == 1
    assert job.run_after > timezone.now()
    assert "service down" in job.last_error
    assert outbox.process_batch()["retry"] == 0

    for _ in range(job.max_attempts - 1):
        make_due()
        outbox.process_batch()

    job.refresh_from_db()
    assert job.status == "failed" and job.attempts == job.max_attempts
    assert not Notification.objects.exists()


def test_backoff_doubles_and_is_capped():
    assert [outbox.backoff(n).total_seconds() for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert outbox.backoff(30) == outbox.MAX_DELAY


def test_approval_request_notifies_leads_but_not_the_assignee(world, open_item):
    item = claim_item(world.lead, open_item)
    OutboxJob.objects.all().delete()
    transition_item(world.lead, item, S.AWAITING_APPROVAL, version=item.version)
    outbox.process_batch()
    assert list(Notification.objects.values_list("user__username", flat=True)) == ["lead2"]


def test_handoff_notifies_old_team_leads_and_previous_assignee(world, open_item):
    compliance_lead = User.objects.create_user(username="c_lead", password="x")
    Membership.objects.create(user=compliance_lead, team=world.compliance, role="lead")

    item = claim_item(world.alice, open_item)
    OutboxJob.objects.all().delete()
    handoff_item(world.lead, item, world.compliance, version=item.version, reason="KYC problem")
    outbox.process_batch()

    recipients = set(Notification.objects.values_list("user__username", flat=True))
    assert recipients == {"lead2", "alice"}
    assert "KYC problem" in Notification.objects.get(user=world.lead2).message
