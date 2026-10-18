from django.utils import timezone
from rest_framework import serializers

from .models import WorkItem
from .queries import ACTIVE, STALE_AFTER


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
