"""
Seed demo data: teams, users with roles, and work items with consistent history.

    python manage.py seed               # 3000 items
    python manage.py seed --items 50    # small dataset
    python manage.py seed --items 50000 # scale test

Safe to re-run: it wipes ALL work-item data and the demo users, then recreates them.
"""

import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ops.models import ActivityEvent, Comment, IdempotencyKey, Membership, Team, WorkItem

User = get_user_model()

PASSWORD = "demo1234"

TEAMS = ["Payments", "Compliance", "Customer Support", "Platform"]

# username -> (is_admin, {team: role})
USERS = {
    "admin": (True, {}),
    # Lead in Payments but only a Viewer in Compliance: shows roles are per team.
    "payments_lead": (False, {"Payments": "lead", "Compliance": "viewer"}),
    "payments_member": (False, {"Payments": "member"}),
    "payments_member2": (False, {"Payments": "member"}),
    "compliance_lead": (False, {"Compliance": "lead"}),
    "compliance_member": (False, {"Compliance": "member"}),
    "support_lead": (False, {"Customer Support": "lead"}),
    "support_member": (False, {"Customer Support": "member"}),
    "platform_lead": (False, {"Platform": "lead"}),
    "platform_member": (False, {"Platform": "member"}),
    # Read-only everywhere.
    "auditor": (False, {team: "viewer" for team in TEAMS}),
}

# Which item types each team mostly deals with.
TEAM_TYPES = {
    "Payments": ["payment", "payment", "customer_issue", "task"],
    "Compliance": ["compliance", "compliance", "task"],
    "Customer Support": ["customer_issue", "customer_issue", "task"],
    "Platform": ["incident", "incident", "task"],
}

TITLES = {
    "payment": [
        "UPI payment failed for order #{n}",
        "Refund not credited for transaction #{n}",
        "Duplicate charge reported on card ending {n4}",
        "Settlement mismatch in batch #{n}",
        "Payment gateway timeout spike (merchant {n4})",
    ],
    "compliance": [
        "KYC document expired for account #{n}",
        "AML alert raised on account #{n}",
        "Quarterly audit evidence request #{n}",
        "GDPR data deletion request #{n}",
        "Missing PAN verification for merchant {n4}",
    ],
    "customer_issue": [
        "Customer unable to log in (ticket #{n})",
        "Order #{n} stuck in processing",
        "Customer requests invoice correction #{n}",
        "Complaint about delayed refund #{n}",
        "Account locked after password reset (ticket #{n})",
    ],
    "incident": [
        "API latency above 2s on checkout service",
        "Database CPU at 95% on replica {n4}",
        "Elevated 5xx errors on payments API",
        "Disk space low on log server {n4}",
        "SSL certificate expiring on api-{n4}",
    ],
    "task": [
        "Update runbook for incident type #{n}",
        "Review access list for team tools",
        "Prepare weekly ops report #{n}",
        "Rotate API keys for vendor {n4}",
        "Clean up stale alerts in monitoring",
    ],
}

# Status path an item walks through to reach each final status.
PATH = ["open", "in_progress", "awaiting_approval", "resolved", "closed"]

STATUS_WEIGHTS = {
    "open": 25,
    "in_progress": 20,
    "awaiting_approval": 10,
    "resolved": 15,
    "closed": 25,
    "cancelled": 5,
}

PRIORITY_WEIGHTS = {"P1": 5, "P2": 20, "P3": 45, "P4": 30}

ACTIVE = {"open", "in_progress", "awaiting_approval"}


class Command(BaseCommand):
    help = "Wipe and recreate demo teams, users and work items."

    def add_arguments(self, parser):
        parser.add_argument("--items", type=int, default=3000, help="Number of work items to create.")

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(42)  # fixed seed: same data on every run
        now = timezone.now()

        self.wipe()
        teams, users, members_by_team, leads_by_team = self.create_teams_and_users()
        creators = [u for name, u in users.items() if name not in ("auditor",)]

        items, plans = [], []
        for _ in range(options["items"]):
            item, plan = self.build_item(rng, now, teams, creators, members_by_team, leads_by_team)
            items.append(item)
            plans.append(plan)

        WorkItem.objects.bulk_create(items, batch_size=1000)

        events, comments = [], []
        for item, plan in zip(items, plans):
            for ev in plan["events"]:
                events.append(ActivityEvent(item=item, **ev))
            for c in plan["comments"]:
                comments.append(Comment(item=item, **c))

        ActivityEvent.objects.bulk_create(events, batch_size=2000)
        Comment.objects.bulk_create(comments, batch_size=2000)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(teams)} teams, {len(users)} users, {len(items)} items, "
            f"{len(events)} activity events, {len(comments)} comments. Password for all users: {PASSWORD}"
        ))

    def wipe(self):
        # Order matters: history references items, items reference teams and users.
        IdempotencyKey.objects.all().delete()
        ActivityEvent.objects.all().delete()
        Comment.objects.all().delete()
        WorkItem.objects.all().delete()
        Membership.objects.all().delete()
        Team.objects.all().delete()
        User.objects.filter(username__in=USERS.keys()).delete()

    def create_teams_and_users(self):
        teams = {name: Team.objects.create(name=name) for name in TEAMS}
        users, members_by_team, leads_by_team = {}, {t: [] for t in TEAMS}, {t: [] for t in TEAMS}

        for username, (is_admin, roles) in USERS.items():
            user = User(username=username, is_superuser=is_admin, is_staff=is_admin)
            user.set_password(PASSWORD)
            user.save()
            users[username] = user
            for team_name, role in roles.items():
                Membership.objects.create(user=user, team=teams[team_name], role=role)
                if role in ("member", "lead"):
                    members_by_team[team_name].append(user)
                if role == "lead":
                    leads_by_team[team_name].append(user)

        return teams, users, members_by_team, leads_by_team

    def build_item(self, rng, now, teams, creators, members_by_team, leads_by_team):
        team_name = rng.choice(TEAMS)
        item_type = rng.choice(TEAM_TYPES[team_name])
        status = rng.choices(list(STATUS_WEIGHTS), weights=STATUS_WEIGHTS.values())[0]
        priority = rng.choices(list(PRIORITY_WEIGHTS), weights=PRIORITY_WEIGHTS.values())[0]

        # Active items are recent (some become SLA-breached or stale);
        # finished items are spread over the last 60 days.
        if status in ACTIVE:
            created_at = now - timedelta(minutes=rng.randint(10, 7 * 24 * 60))
        else:
            created_at = now - timedelta(minutes=rng.randint(7 * 24 * 60, 60 * 24 * 60))

        # Most items are raised inside the team; about 1 in 4 by someone from another team.
        team_people = members_by_team[team_name]
        creator = rng.choice(team_people) if rng.random() < 0.75 else rng.choice(creators)
        lead = rng.choice(leads_by_team[team_name])

        title = rng.choice(TITLES[item_type]).format(n=rng.randint(10000, 99999), n4=rng.randint(1000, 9999))

        # Build the history first, then derive the item's fields from it,
        # so the seeded item and its history always agree.
        events, comments = [], []
        clock = created_at

        def step(max_hours):
            nonlocal clock
            end = now if status in ACTIVE else min(now, created_at + timedelta(days=6))
            remaining = (end - clock).total_seconds()
            clock += timedelta(seconds=rng.uniform(0, min(remaining, max_hours * 3600)) if remaining > 0 else 0)
            return clock

        events.append(dict(actor=creator, type="created", new_value=title[:200], created_at=clock))

        assignee = None
        resolved_at = None
        if status == "cancelled":
            events.append(dict(
                actor=lead, type="status_changed", old_value="open", new_value="cancelled",
                reason="Duplicate of an existing item.", created_at=step(12),
            ))
        else:
            target = PATH.index(status)
            if target >= 1:
                assignee = rng.choice(team_people)
                events.append(dict(actor=assignee, type="claimed", new_value=assignee.username, created_at=step(6)))
            for i in range(1, target + 1):
                old, new = PATH[i - 1], PATH[i]
                # Approval and closing are Lead-only steps.
                actor = lead if new in ("resolved", "closed") else assignee
                at = step(24)
                events.append(dict(actor=actor, type="status_changed", old_value=old, new_value=new, created_at=at))
                if new == "resolved":
                    resolved_at = at

        if rng.random() < 0.15:
            author = assignee or creator
            body = rng.choice([
                "Looking into this now.",
                "Customer has been informed, waiting on their reply.",
                "Escalated to the vendor.",
                "Root cause identified, fix in progress.",
                "Any update on this?",
            ])
            at = step(12)
            comments.append(dict(author=author, body=body, created_at=at))
            events.append(dict(actor=author, type="commented", new_value=body[:200], created_at=at))

        item = WorkItem(
            title=title,
            description=f"Seeded {item_type.replace('_', ' ')} item for the {team_name} team.",
            type=item_type,
            priority=priority,
            status=status,
            team=teams[team_name],
            assignee=assignee,
            created_by=creator,
            created_at=created_at,
            resolved_at=resolved_at,
            last_activity_at=events[-1]["created_at"],
            # One version per change to the item: "created" is version 1, every later change adds one.
            # Comments don't change the item, so they don't bump the version.
            version=sum(1 for e in events if e["type"] != "commented"),
        )
        item.sla_due_at = item.compute_sla_due_at()

        return item, {"events": events, "comments": comments}
