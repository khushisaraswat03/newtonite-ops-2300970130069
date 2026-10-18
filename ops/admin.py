from django.contrib import admin

from .models import ActivityEvent, Comment, IdempotencyKey, Membership, Notification, OutboxJob, Team, WorkItem


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ["name"]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "team", "role"]
    list_filter = ["team", "role"]


@admin.register(WorkItem)
class WorkItemAdmin(admin.ModelAdmin):
    list_display = ["id", "title", "team", "type", "priority", "status", "assignee", "version"]
    list_filter = ["team", "status", "priority", "type"]
    search_fields = ["title"]


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ["item", "actor", "type", "old_value", "new_value", "created_at"]
    list_filter = ["type"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Comment)
admin.site.register(IdempotencyKey)


@admin.register(OutboxJob)
class OutboxJobAdmin(admin.ModelAdmin):
    list_display = ["id", "kind", "status", "attempts", "run_after", "last_error", "created_at"]
    list_filter = ["status", "kind"]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["user", "message", "created_at", "read_at"]
