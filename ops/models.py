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

    due_date = models.DateField(null=True, blank=True)
    sla_due_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    version = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(default=timezone.now)
    team_since = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    last_activity_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["team", "status", "priority", "sla_due_at", "id"], name="item_team_status_idx"),
            models.Index(fields=["priority", "sla_due_at", "id"], name="item_urgency_idx"),
            models.Index(fields=["assignee", "status"], name="item_assignee_status_idx"),
            models.Index(fields=["created_by"], name="item_created_by_idx"),
            models.Index(fields=["status", "sla_due_at"], name="item_status_sla_idx"),
            GinIndex(
                SearchVector("title", "description", config="english"),
                name="item_search_idx",
            ),
        ]

    def __str__(self):
        return f"#{self.pk} {self.title}"

    def compute_sla_due_at(self, priority=None):
        return self.team_since + self.SLA[priority or self.priority]


class ActivityEvent(models.Model):
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
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=100)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_idempotency_key_per_user"),
        ]


class OutboxJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=50)
    payload = models.JSONField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    run_after = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "run_after", "id"], name="outbox_due_idx"),
        ]


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    item = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name="+")
    message = models.CharField(max_length=300)
    job = models.OneToOneField(OutboxJob, on_delete=models.SET_NULL, null=True, related_name="notification")
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "read_at", "-created_at"], name="notif_user_unread_idx"),
        ]
