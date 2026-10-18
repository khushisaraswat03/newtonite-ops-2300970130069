import uuid
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from . import services
from .idempotency import KeyReused, fingerprint, run_once
from .models import Notification, Team, WorkItem
from .permissions import can_raise, check, team_roles
from .queries import BadQuery, apply_view, filter_items, lead_team_ids, paginate
from .workflow import TRANSITIONS, TransitionError, allowed_actions

S = WorkItem.Status
TIMELINE_PAGE_SIZE = 50

DEMO_USERS = [
    "payments_member", "payments_member2", "payments_lead", "compliance_lead",
    "support_member", "platform_lead", "auditor", "admin",
]

TRANSITION_LABELS = {
    (S.IN_PROGRESS, S.AWAITING_APPROVAL): "Submit for approval",
    (S.AWAITING_APPROVAL, S.RESOLVED): "Approve ✓",
    (S.AWAITING_APPROVAL, S.IN_PROGRESS): "Send back",
    (S.RESOLVED, S.CLOSED): "Close",
    (S.RESOLVED, S.IN_PROGRESS): "Reopen (I'll continue)",
    (S.CLOSED, S.IN_PROGRESS): "Reopen (I'll continue)",
    (S.RESOLVED, S.OPEN): "Reopen to team queue",
    (S.CLOSED, S.OPEN): "Reopen to team queue",
    (S.OPEN, S.CANCELLED): "Cancel item",
    (S.IN_PROGRESS, S.CANCELLED): "Cancel item",
    (S.AWAITING_APPROVAL, S.CANCELLED): "Cancel item",
}

DASHBOARD_VIEWS = [
    ("assigned_to_me", "Assigned to me", "In progress with you", "🙋", "t1"),
    ("unassigned_urgent", "Unassigned urgent", "P1/P2 nobody has claimed", "🔥", "t2"),
    ("awaiting_my_approval", "Awaiting my approval", "Ready for your review", "🧐", "t3"),
    ("sla_breached", "SLA breached", "Past their deadline", "⏰", "t4"),
    ("stale", "Gone quiet", "No activity for 3+ days", "💤", "t5"),
    ("raised_by_me", "Raised by me", "Things you reported", "📣", "t6"),
]


class OpsLoginView(LoginView):
    template_name = "ops/login.html"
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show_demo_users"] = settings.DEBUG
        ctx["demo_users"] = DEMO_USERS
        return ctx


def _greeting():
    hour = timezone.localtime().hour
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"


def _roles(user):
    teams = dict(Team.objects.values_list("id", "name"))
    return sorted(({"team": teams[t], "role": r} for t, r in team_roles(user).items()), key=lambda x: x["team"])


@login_required
def dashboard(request):
    user = request.user
    is_approver = user.is_superuser or bool(lead_team_ids(user))
    views = [v for v in DASHBOARD_VIEWS if v[0] != "awaiting_my_approval" or is_approver]

    tiles = [
        {"view": v, "label": label, "hint": hint, "emoji": emoji, "css": css,
         "count": apply_view(filter_items(user, {}), user, v).count()}
        for v, label, hint, emoji, css in views
    ]

    selected = request.GET.get("view") or next((t["view"] for t in tiles if t["count"]), "assigned_to_me")
    try:
        items, next_cursor = paginate(filter_items(user, {"view": selected}), request.GET.get("cursor"))
    except BadQuery as e:
        messages.error(request, str(e))
        return redirect("dashboard")

    return render(request, "ops/dashboard.html", {
        "nav": "dashboard",
        "greeting": _greeting(),
        "roles": _roles(user),
        "tiles": tiles,
        "selected": selected,
        "selected_label": next((t["label"] for t in tiles if t["view"] == selected), ""),
        "items": items,
        "next_cursor": next_cursor,
    })


@login_required
def item_list(request):
    try:
        qs = filter_items(request.user, request.GET)
        items, next_cursor = paginate(qs, request.GET.get("cursor"))
    except BadQuery as e:
        messages.error(request, str(e))
        return redirect("item_list")

    filters_on = any(request.GET.get(k) for k in ("q", "status", "priority", "type", "team", "assignee"))
    return render(request, "ops/item_list.html", {
        "nav": "items",
        "items": items,
        "next_cursor": next_cursor,
        "filters_on": filters_on,
        "f": request.GET,
        "teams": Team.objects.order_by("name"),
        "statuses": S.choices,
        "priorities": WorkItem.Priority.choices,
        "types": WorkItem.Type.choices,
    })


def _new_key():
    return uuid.uuid4().hex


@login_required
def item_create(request):
    user = request.user
    if not can_raise(user):
        messages.error(request, "Viewers can't raise items. Ask a Member or Lead to raise it for you.")
        return redirect("dashboard")

    values, errors = {}, {}
    if request.method == "POST":
        values = request.POST
        key = request.POST.get("idempotency_key") or _new_key()
        try:
            team = Team.objects.get(pk=request.POST.get("team") or 0)
            due = date.fromisoformat(request.POST["due_date"]) if request.POST.get("due_date") else None
            data = dict(
                team=team, title=request.POST.get("title", ""), type=request.POST.get("type", ""),
                priority=request.POST.get("priority", ""), description=request.POST.get("description", ""),
                due_date=due,
            )

            def action():
                item = services.create_item(user, **data)
                return 302, {"item_id": item.id}

            _, body, replayed = run_once(user, key, fingerprint("create", {k: str(v) for k, v in data.items()}), action)
            if not replayed:
                messages.success(request, "Item raised! It's now in the team's queue. 🎉")
            return redirect("item_detail", pk=body["item_id"])
        except Team.DoesNotExist:
            errors = {"team": ["Pick a team."]}
        except ValueError:
            errors = {"due_date": ["Enter a valid date."]}
        except ValidationError as e:
            errors = dict(e.message_dict)
            if "__all__" in errors:
                errors["form"] = errors.pop("__all__")
        except KeyReused:
            errors = {"form": ["This form was already submitted. Please reload the page."]}

    return render(request, "ops/item_create.html", {
        "nav": "create",
        "v": values,
        "errors": errors,
        "teams": Team.objects.order_by("name"),
        "my_teams": set(team_roles(user)),
        "priorities": WorkItem.Priority.choices,
        "types": WorkItem.Type.choices,
        "idempotency_key": _new_key(),
    })


def _load_item(request, pk):
    item = WorkItem.objects.select_related("team", "assignee", "created_by").filter(pk=pk).first()
    if item is None:
        raise Http404("Item not found.")
    check(request.user, "view", item)
    return item


def _detail_context(request, item, conflict=None, attempted=None):
    user = request.user
    events_qs = item.events.select_related("actor").order_by("-id")
    before = request.GET.get("before")
    if before and before.isdigit():
        events_qs = events_qs.filter(id__lt=int(before))
    rows = list(events_qs[: TIMELINE_PAGE_SIZE + 1])
    events = rows[:TIMELINE_PAGE_SIZE]

    comments = {c.created_at: c.body for c in item.comments.filter(created_at__in=[e.created_at for e in events if e.type == "commented"])}
    for e in events:
        e.comment_body = comments.get(e.created_at) if e.type == "commented" else None

    allowed = allowed_actions(user, item)
    transitions = [
        {"status": new, "label": TRANSITION_LABELS[(item.status, new)],
         "needs_reason": TRANSITIONS[(item.status, new)]["reason"],
         "danger": new == S.CANCELLED}
        for new in allowed["transitions"]
    ]
    return {
        "item": item,
        "allowed": allowed,
        "transitions": transitions,
        "events": events,
        "older_before": events[-1].id if len(rows) > TIMELINE_PAGE_SIZE else None,
        "teams": Team.objects.exclude(pk=item.team_id).order_by("name"),
        "priorities": WorkItem.Priority.choices,
        "loaded_at": timezone.now().isoformat(),
        "idempotency_key": _new_key(),
        "conflict": conflict,
        "attempted": attempted or {},
        "has_actions": any(v for k, v in allowed.items() if k != "comment"),
    }


@login_required
def item_detail(request, pk):
    item = _load_item(request, pk)
    return render(request, "ops/item_detail.html", _detail_context(request, item))


@login_required
@require_POST
def item_action(request, pk, action):
    user, post = request.user, request.POST
    item = _load_item(request, pk)
    version = int(post["version"]) if post.get("version", "").isdigit() else -1
    reason = post.get("reason", "")

    def run():
        if action == "claim":
            services.claim_item(user, item)
            return "You claimed it. It's all yours! 🙋"
        if action == "release":
            services.release_item(user, item)
            return "Released back to the team queue."
        if action == "unassign":
            services.unassign_item(user, item, reason)
            return "Unassigned. The item is back in the queue."
        if action == "transition":
            services.transition_item(user, item, post.get("status", ""), version=version, reason=reason)
            return "Status updated."
        if action == "edit":
            due = date.fromisoformat(post["due_date"]) if post.get("due_date") else None
            services.update_item(user, item, version=version, priority=post.get("priority") or None,
                                 due_date=due, reason=reason)
            return "Changes saved."
        if action == "handoff":
            team = Team.objects.filter(pk=post.get("team") or 0).first()
            if team is None:
                raise ValidationError({"team": "Pick a team to hand off to."})
            services.handoff_item(user, item, team, version=version, reason=reason)
            return f"Handed off to {team.name}. 🤝"
        if action == "comment":
            services.add_comment(user, item, post.get("body", ""))
            return "Comment added."
        raise Http404("Unknown action.")

    key = post.get("idempotency_key") or _new_key()
    try:
        _, body, replayed = run_once(
            user, key, fingerprint(action, pk, {k: v for k, v in post.items() if k != "csrfmiddlewaretoken"}),
            lambda: (302, {"message": run()}),
        )
        if not replayed:
            messages.success(request, body["message"])
    except services.ConflictError as e:
        latest = _load_item(request, pk)
        loaded_at = parse_datetime(post.get("loaded_at", "")) or latest.updated_at
        changes = list(latest.events.select_related("actor").filter(created_at__gt=loaded_at).order_by("id"))
        ctx = _detail_context(request, latest, conflict={"message": str(e), "changes": changes, "action": action},
                              attempted=post)
        return render(request, "ops/item_detail.html", ctx, status=409)
    except (ValidationError, TransitionError) as e:
        msg = "; ".join(e.messages) if isinstance(e, ValidationError) else str(e)
        messages.error(request, msg)
    except PermissionDenied as e:
        messages.error(request, str(e) or "You're not allowed to do that.")
    except ValueError:
        messages.error(request, "Enter a valid date.")
    except KeyReused:
        messages.error(request, "This form was already submitted. Please reload the page and try again.")

    try:
        check(user, "view", WorkItem.objects.get(pk=pk))
    except Http404:
        return redirect("dashboard")
    return redirect("item_detail", pk=pk)


@login_required
def notifications(request):
    notes = list(Notification.objects.filter(user=request.user).select_related("item").order_by("-created_at")[:50])
    for n in notes:
        n.is_new = n.read_at is None
    Notification.objects.filter(user=request.user, read_at__isnull=True).update(read_at=timezone.now())
    return render(request, "ops/notifications.html", {"nav": "notifications", "notes": notes, "unread_count": 0})
