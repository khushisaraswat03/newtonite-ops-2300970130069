from django.test import Client

from ops import outbox
from ops.models import Notification, WorkItem
from ops.services import claim_item, create_item


def client_for(user):
    c = Client()
    c.force_login(user)
    return c


def test_pages_require_login(world):
    r = Client().get("/")
    assert r.status_code == 302 and "/login/" in r["Location"]


def test_dashboard_list_and_create_pages_render(world, open_item):
    alice = client_for(world.alice)
    assert alice.get("/").status_code == 200
    assert b"Refund stuck" in alice.get("/items/").content
    assert alice.get("/items/new/").status_code == 200


def test_outsider_gets_404_on_item_page_and_actions(world, open_item):
    outsider = client_for(world.outsider)
    assert outsider.get(f"/items/{open_item.id}/").status_code == 404
    assert outsider.post(f"/items/{open_item.id}/claim/").status_code == 404
    open_item.refresh_from_db()
    assert open_item.assignee is None


def test_double_submitted_create_form_makes_one_item(world):
    alice = client_for(world.alice)
    form = {"idempotency_key": "form-abc", "team": world.payments.id, "title": "Card declined",
            "type": "payment", "priority": "P2", "description": "", "due_date": ""}
    first = alice.post("/items/new/", form)
    second = alice.post("/items/new/", form)
    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"]
    assert WorkItem.objects.filter(title="Card declined").count() == 1


def test_claim_button(world, open_item):
    r = client_for(world.bob).post(f"/items/{open_item.id}/claim/", {"idempotency_key": "c1"})
    assert r.status_code == 302
    open_item.refresh_from_db()
    assert open_item.assignee == world.bob


def test_stale_edit_shows_conflict_box_and_keeps_input(world, open_item):
    alice = client_for(world.alice)
    alice.post(f"/items/{open_item.id}/claim/", {"idempotency_key": "c1"})
    alice.post(f"/items/{open_item.id}/edit/", {"idempotency_key": "e1", "version": 2, "priority": "P1"})

    r = client_for(world.lead).post(f"/items/{open_item.id}/edit/", {
        "idempotency_key": "e2", "version": 2, "priority": "P1", "due_date": "2026-12-01",
        "loaded_at": "2000-01-01T00:00:00+00:00",
    })
    assert r.status_code == 409
    html = r.content.decode()
    assert "Your change was <u>not</u> saved" in html
    assert "changed priority P2 → P1" in html
    assert 'value="2026-12-01"' in html
    open_item.refresh_from_db()
    assert open_item.due_date is None


def test_rule_violation_shows_message_instead_of_crashing(world, open_item):
    r = client_for(world.lead).post(
        f"/items/{open_item.id}/transition/", {"idempotency_key": "t1", "version": 1, "status": "cancelled"}, follow=True,
    )
    assert b"A reason is required" in r.content


def test_notifications_page_marks_as_read(world):
    item = create_item(world.outsider, team=world.payments, title="Wrong fee", type="payment", priority="P3")
    claim_item(world.alice, item)
    outbox.process_batch()
    c = client_for(world.outsider)
    assert b'class="badge">1<' in c.get("/").content
    assert b"alice picked up" in c.get("/notifications/").content
    assert not Notification.objects.filter(read_at__isnull=True).exists()
