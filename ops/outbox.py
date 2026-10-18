import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Membership, Notification, OutboxJob

log = logging.getLogger("ops.outbox")

BASE_DELAY = timedelta(seconds=10)
MAX_DELAY = timedelta(minutes=10)


def enqueue(kind, **payload):
    return OutboxJob.objects.create(kind=kind, payload=payload)


def notify(user_ids, item, message, exclude=None):
    for user_id in set(user_ids) - {exclude, None}:
        enqueue("notify", user_id=user_id, item_id=item.pk, message=message)


def lead_ids(team_id):
    return list(Membership.objects.filter(team_id=team_id, role="lead").values_list("user_id", flat=True))


def handle_notify(job):
    p = job.payload
    Notification.objects.get_or_create(
        job=job, defaults={"user_id": p["user_id"], "item_id": p["item_id"], "message": p["message"]},
    )
    log.info("Notified user %s: %s", p["user_id"], p["message"])


HANDLERS = {"notify": handle_notify}


def backoff(attempts):
    return min(BASE_DELAY * (2 ** (attempts - 1)), MAX_DELAY)


def process_batch(limit=20):
    counts = {"done": 0, "retry": 0, "failed": 0}
    for _ in range(limit):
        with transaction.atomic():
            job = (
                OutboxJob.objects.select_for_update(skip_locked=True)
                .filter(status=OutboxJob.Status.PENDING, run_after__lte=timezone.now())
                .order_by("run_after", "id")
                .first()
            )
            if job is None:
                break

            try:
                with transaction.atomic():
                    HANDLERS[job.kind](job)
            except Exception as e:
                job.attempts += 1
                job.last_error = f"{type(e).__name__}: {e}"
                if job.attempts >= job.max_attempts:
                    job.status = OutboxJob.Status.FAILED
                    job.processed_at = timezone.now()
                    counts["failed"] += 1
                    log.error("Job %s failed permanently: %s", job.pk, job.last_error)
                else:
                    job.run_after = timezone.now() + backoff(job.attempts)
                    counts["retry"] += 1
                    log.warning("Job %s failed (attempt %s), retrying at %s", job.pk, job.attempts, job.run_after)
            else:
                job.attempts += 1
                job.status = OutboxJob.Status.DONE
                job.processed_at = timezone.now()
                counts["done"] += 1
            job.save()
    return counts
