from datetime import timedelta

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL


class Team(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Membership(models.Model):
    """A user's role inside one team. Admin is not a team role: it is user.is_superuser."""

    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        MEMBER = "member", "Member"
        LEAD = "lead", "Lead"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=Role.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "team"], name="one_role_per_team"),
        ]

    def __str__(self):
        return f"{self.user} - {self.team} ({self.role})"


class WorkItem(models.Model):
    class Type(models.TextChoices):
        INCIDENT = "incident", "Incident"
        CUSTOMER_ISSUE = "customer_issue", "Customer issue"
        PAYMENT = "payment", "Payment"
        COMPLIANCE = "compliance", "Compliance"
        TASK = "task", "Task"

    class Priority(models.TextChoices):
        P1 = "P1", "P1 - Critical"
        P2 = "P2", "P2 - High"
        P3 = "P3", "P3 - Medium"
        P4 = "P4", "P4 - Low"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting Approval"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"
        CANCELLED = "cancelled", "Cancelled"

    # How long each priority has before it breaches its SLA.
    SLA = {
        Priority.P1: timedelta(hours=4),
        Priority.P2: timedelta(hours=24),
        Priority.P3: timedelta(days=3),
        Priority.P4: timedelta(days=5),
    }

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    type = models.CharField(max_length=20, choices=Type.choices)
    priority = models.CharField(max_length=2, choices=Priority.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="items")
    assignee = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="assigned_items"
    )
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_items")

    # due_date: optional business deadline chosen by the creator.
    # sla_due_at: system deadline derived from priority (see SLA above).
    due_date = models.DateField(null=True, blank=True)
    sla_due_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    # Optimistic locking: every write must send the version it read; the
    # service layer only updates WHERE version = <sent version>, then bumps it.
    version = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(default=timezone.now)
    # When the current team received the item (creation, or the latest handoff).
    # The SLA is measured from here, so a team isn't blamed for time another team had it;
    # created_at still shows how long the requester has been waiting overall.
    team_since = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    # Bumped by every activity event (comments too); drives stale detection.
    last_activity_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            # Team list views, filtered by status, newest first (cursor pagination order).
            models.Index(fields=["team", "status", "-created_at", "-id"], name="item_team_status_idx"),
            # "Assigned to me".
            models.Index(fields=["assignee", "status"], name="item_assignee_status_idx"),
            # "Items I raised" for people outside the team.
            models.Index(fields=["created_by"], name="item_created_by_idx"),
            # "SLA breached": open items whose deadline has passed.
            models.Index(fields=["status", "sla_due_at"], name="item_status_sla_idx"),
            # Full-text search over title + description.
            GinIndex(
                SearchVector("title", "description", config="english"),
                name="item_search_idx",
            ),
        ]

    def __str__(self):
        return f"#{self.pk} {self.title}"

    def compute_sla_due_at(self, priority=None):
        """SLA runs from when the current team got the item; a priority change recalculates it from there."""
        return self.team_since + self.SLA[priority or self.priority]


class ActivityEvent(models.Model):
    """Append-only history. Written in the same transaction as the change it describes."""

    class Type(models.TextChoices):
        CREATED = "created", "Created"
        EDITED = "edited", "Edited"
        STATUS_CHANGED = "status_changed", "Status changed"
        PRIORITY_CHANGED = "priority_changed", "Priority changed"
        CLAIMED = "claimed", "Claimed"
        RELEASED = "released", "Released"
        UNASSIGNED = "unassigned", "Unassigned"
        HANDED_OFF = "handed_off", "Handed off to another team"
        COMMENTED = "commented", "Commented"

    item = models.ForeignKey(WorkItem, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    type = models.CharField(max_length=20, choices=Type.choices)
    old_value = models.CharField(max_length=200, blank=True)
    new_value = models.CharField(max_length=200, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["item", "created_at"], name="event_item_time_idx"),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("Activity events are append-only and cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Activity events are append-only and cannot be deleted.")


class Comment(models.Model):
    item = models.ForeignKey(WorkItem, on_delete=models.PROTECT, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["item", "created_at"], name="comment_item_time_idx"),
        ]


class IdempotencyKey(models.Model):
    """Remembers the response to a request so a retry with the same key gets the same answer."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=100)
    # Hash of method + path + body: the same key reused for a different request is rejected.
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_idempotency_key_per_user"),
        ]
