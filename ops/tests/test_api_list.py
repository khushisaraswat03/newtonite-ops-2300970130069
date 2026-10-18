import pytest
from rest_framework.test import APIClient

from ops.models import WorkItem
from ops.services import claim_item, create_item, transition_item

S = WorkItem.Status


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def ids(response):
    return [row["id"] for row in response.json()["results"]]


def test_login_returns_token_that_works(world):
    c = APIClient()
    assert c.post("/api/auth/token/", {"username": "alice", "password": "wrong"}).status_code == 400

    token = c.post("/api/auth/token/", {"username": "alice", "password": "x"}).json()["token"]
    c.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    me = c.get("/api/me/").json()
    assert me["username"] == "alice"
    assert me["teams"] == [{"id": world.payments.id, "name": "Payments", "role": "member"}]


def test_list_requires_login(world):
    assert APIClient().get("/api/items/").status_code == 401


def test_list_only_shows_items_user_may_see(world, open_item):
    compliance_item = create_item(world.outsider, team=world.compliance, title="Audit", type="compliance", priority="P3")

    assert ids(client_for(world.alice).get("/api/items/")) == [open_item.id]
    assert ids(client_for(world.outsider).get("/api/items/")) == [compliance_item.id]
    assert set(ids(client_for(world.admin).get("/api/items/"))) == {open_item.id, compliance_item.id}


def test_most_urgent_first(world):
    p3 = create_item(world.alice, team=world.payments, title="Low", type="task", priority="P3")
    p1 = create_item(world.alice, team=world.payments, title="Fire", type="incident", priority="P1")
    p2 = create_item(world.alice, team=world.payments, title="Mid", type="task", priority="P2")
    assert ids(client_for(world.alice).get("/api/items/")) == [p1.id, p2.id, p3.id]


def test_cursor_pagination_walks_every_item_once(world):
    for i in range(30):
        create_item(world.alice, team=world.payments, title=f"Item {i}", type="task", priority=["P1", "P2", "P3"][i % 3])
    c = client_for(world.alice)

    first = c.get("/api/items/", {"page_size": 25}).json()
    assert len(first["results"]) == 25 and first["next_cursor"]

    second = c.get("/api/items/", {"page_size": 25, "cursor": first["next_cursor"]}).json()
    assert len(second["results"]) == 5 and second["next_cursor"] is None

    seen = [r["id"] for r in first["results"] + second["results"]]
    assert len(seen) == len(set(seen)) == 30
    assert [r["priority"] for r in first["results"] + second["results"]] == sorted(r["priority"] for r in first["results"] + second["results"])


def test_page_size_is_capped(world):
    for i in range(3):
        create_item(world.alice, team=world.payments, title=f"Item {i}", type="task", priority="P3")
    r = client_for(world.alice).get("/api/items/", {"page_size": 5000})
    assert r.status_code == 200  # capped at MAX_PAGE_SIZE rather than loading everything


def test_bad_cursor_and_filters_give_400(world):
    c = client_for(world.alice)
    assert c.get("/api/items/", {"cursor": "garbage"}).status_code == 400
    assert c.get("/api/items/", {"status": "nonsense"}).status_code == 400
    assert c.get("/api/items/", {"view": "nonsense"}).status_code == 400


def test_filters_and_search(world):
    refund = create_item(world.alice, team=world.payments, title="UPI refund not credited", type="payment", priority="P2")
    create_item(world.alice, team=world.payments, title="Update runbook", type="task", priority="P4")
    c = client_for(world.alice)

    assert ids(c.get("/api/items/", {"q": "refund"})) == [refund.id]
    assert ids(c.get("/api/items/", {"type": "payment"})) == [refund.id]
    assert ids(c.get("/api/items/", {"priority": "P1,P2"})) == [refund.id]


def test_dashboard_views(world):
    urgent = create_item(world.alice, team=world.payments, title="Gateway down", type="incident", priority="P1")
    mine = create_item(world.alice, team=world.payments, title="Mine", type="task", priority="P3")
    claim_item(world.bob, mine)
    waiting = create_item(world.alice, team=world.payments, title="Needs approval", type="task", priority="P3")
    claim_item(world.alice, waiting)
    waiting.refresh_from_db()
    transition_item(world.alice, waiting, S.AWAITING_APPROVAL, version=waiting.version)

    assert ids(client_for(world.bob).get("/api/items/", {"view": "assigned_to_me"})) == [mine.id]
    assert ids(client_for(world.bob).get("/api/items/", {"view": "unassigned_urgent"})) == [urgent.id]
    assert ids(client_for(world.lead).get("/api/items/", {"view": "awaiting_my_approval"})) == [waiting.id]
    # A member is not an approver, and an assignee never sees their own work as "awaiting my approval".
    assert ids(client_for(world.bob).get("/api/items/", {"view": "awaiting_my_approval"})) == []
    assert ids(client_for(world.alice).get("/api/items/", {"view": "awaiting_my_approval"})) == []
