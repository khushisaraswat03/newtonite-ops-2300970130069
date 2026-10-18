from django.utils import timezone
from rest_framework import serializers

from .models import ActivityEvent, Comment, Team, WorkItem
from .permissions import can
from .queries import ACTIVE, STALE_AFTER
from .workflow import allowed_transitions


# ---- Output: what the API sends back ----

class WorkItemListSerializer(serializers.ModelSerializer):
    team_name = serializers.CharField(source="team.name")
    assignee = serializers.CharField(source="assignee.username", default=None)
    created_by = serializers.CharField(source="created_by.username")
    sla_breached = serializers.SerializerMethodField()
    is_stale = serializers.SerializerMethodField()

    class Meta:
        model = WorkItem
        fields = [
            "id", "title", "type", "priority", "status",
            "team", "team_name", "assignee", "created_by",
            "due_date", "sla_due_at", "sla_breached", "is_stale",
            "version", "created_at", "team_since", "last_activity_at",
        ]

    def get_sla_breached(self, item):
        return item.status in ACTIVE and item.sla_due_at < timezone.now()

    def get_is_stale(self, item):
        return item.status in ACTIVE and item.last_activity_at < timezone.now() - STALE_AFTER


class WorkItemDetailSerializer(WorkItemListSerializer):
    allowed = serializers.SerializerMethodField()

    class Meta(WorkItemListSerializer.Meta):
        fields = WorkItemListSerializer.Meta.fields + ["description", "resolved_at", "allowed"]

    def get_allowed(self, item):
        """What the current user may do right now, so a client can show only valid buttons.
        The server still checks every action; this is a convenience, not security."""
        user = self.context["user"]
        return {
            "claim": can(user, "claim", item) and item.status == WorkItem.Status.OPEN and item.assignee_id is None,
            "release": item.assignee_id == user.id and item.status == WorkItem.Status.IN_PROGRESS,
            "unassign": can(user, "unassign", item) and item.assignee_id is not None
                        and item.status == WorkItem.Status.IN_PROGRESS,
            "edit": can(user, "edit", item),
            "handoff": can(user, "handoff", item),
            "comment": can(user, "comment", item),
            "transitions": allowed_transitions(user, item),
        }


class ActivityEventSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source="actor.username")

    class Meta:
        model = ActivityEvent
        fields = ["id", "type", "actor", "old_value", "new_value", "reason", "created_at"]


class CommentSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username")

    class Meta:
        model = Comment
        fields = ["id", "author", "body", "created_at"]


# ---- Input: validating what the client sends ----
# These only check shape and types; business rules live in services.py.

class CreateItemInput(serializers.Serializer):
    team = serializers.PrimaryKeyRelatedField(queryset=Team.objects.all())
    title = serializers.CharField(max_length=200)
    type = serializers.ChoiceField(choices=WorkItem.Type.choices)
    priority = serializers.ChoiceField(choices=WorkItem.Priority.choices)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    due_date = serializers.DateField(required=False, allow_null=True, default=None)


class EditItemInput(serializers.Serializer):
    version = serializers.IntegerField()
    priority = serializers.ChoiceField(choices=WorkItem.Priority.choices, required=False)
    due_date = serializers.DateField(required=False)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class TransitionInput(serializers.Serializer):
    version = serializers.IntegerField()
    status = serializers.ChoiceField(choices=WorkItem.Status.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class HandoffInput(serializers.Serializer):
    version = serializers.IntegerField()
    team = serializers.PrimaryKeyRelatedField(queryset=Team.objects.all())
    reason = serializers.CharField(allow_blank=True)


class ReasonInput(serializers.Serializer):
    reason = serializers.CharField(allow_blank=True)


class CommentInput(serializers.Serializer):
    body = serializers.CharField()
