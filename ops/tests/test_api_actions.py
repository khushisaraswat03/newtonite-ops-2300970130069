import pytest
from rest_framework.test import APIClient

from ops.models import WorkItem


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def url(item, action=""):
    return f"/api/items/{item.id}/{action + '/' if action else ''}"


def test_user_from_another_team_gets_404_on_read_and_write(world, open_item):
    outsider = client_for(world.outsider)

    responses = [
        outsider.get(url(open_item)),
        outsider.get(url(open_item, "activity")),
        outsider.patch(url(open_item), {"version": 1, "priority": "P1"}, format="json"),
        outsider.post(url(open_item, "claim")),
        outsider.post(url(open_item, "comments"), {"body": "hi"}, format="json"),
        outsider.post(url(open_item, "transition"), {"version": 1, "status": "cancelled", "reason": "x"}, format="json"),
        outsider.post(url(open_item, "handoff"), {"version": 1, "team": world.compliance.id, "reason": "x"}, format="json"),
    ]
    assert [r.status_code for r in responses] == [404] * len(responses)

    open_item.refresh_from_db()
    assert open_item.version == 1 and open_item.assignee is None


def test_hidden_item_looks_exactly_like_a_missing_one(world, open_item):
    outsider = client_for(world.outsider)
    hidden = outsider.get(url(open_item))
    missing = outsider.get("/api/items/999999/")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


def test_viewer_in_team_can_read_but_gets_403_on_write(world, open_item):
    viewer = client_for(world.viewer)
    assert viewer.get(url(open_item)).status_code == 200
    assert viewer.post(url(open_item, "claim")).status_code == 403
    assert viewer.post(url(open_item, "comments"), {"body": "hi"}, format="json").status_code == 403
    assert viewer.post("/api/items/", {
        "team": world.payments.id, "title": "x", "type": "task", "priority": "P3",
    }, format="json").status_code == 403


def test_create_item(world):
    r = client_for(world.outsider).post("/api/items/", {
        "team": world.payments.id, "title": "Wrong fee charged", "type": "payment", "priority": "P2",
    }, format="json")
    assert r.status_code == 201
    body = r.json()
    assert body["team_name"] == "Payments" and body["status"] == "open" and body["version"] == 1
    assert body["allowed"]["claim"] is False
    assert body["allowed"]["comment"] is True


def test_create_with_invalid_input_returns_field_errors(world):
    r = client_for(world.alice).post("/api/items/", {"team": world.payments.id, "title": "x", "type": "task", "priority": "P9"}, format="json")
    assert r.status_code == 400
    assert "priority" in r.json()["errors"]


def test_detail_shows_allowed_actions(world, open_item):
    allowed = client_for(world.bob).get(url(open_item)).json()["allowed"]
    assert allowed["claim"] is True
    assert allowed["edit"] is False and allowed["handoff"] is False


def test_second_claim_gets_409_with_latest_item(world, open_item):
    assert client_for(world.alice).post(url(open_item, "claim")).status_code == 200
    r = client_for(world.bob).post(url(open_item, "claim"))
    assert r.status_code == 409
    assert r.json()["current"]["assignee"] == "alice"


def test_patch_requires_version_and_rejects_stale_one(world, open_item):
    alice = client_for(world.alice)
    alice.post(url(open_item, "claim"))

    assert alice.patch(url(open_item), {"priority": "P1"}, format="json").status_code == 400
    assert alice.patch(url(open_item), {"version": 2, "priority": "P1"}, format="json").status_code == 200

    r = alice.patch(url(open_item), {"version": 2, "due_date": "2026-12-01"}, format="json")
    assert r.status_code == 409
    assert r.json()["current"]["version"] == 3


def test_invalid_transition_is_400_and_forbidden_one_is_403(world, open_item):
    assert client_for(world.lead).post(url(open_item, "transition"), {"version": 1, "status": "closed"}, format="json").status_code == 400
    assert client_for(world.bob).post(url(open_item, "transition"), {"version": 1, "status": "cancelled", "reason": "x"}, format="json").status_code == 403


def test_full_flow_through_api(world, open_item):
    alice, lead = client_for(world.alice), client_for(world.lead)
    v = alice.post(url(open_item, "claim")).json()["version"]
    v = alice.post(url(open_item, "transition"), {"version": v, "status": "awaiting_approval"}, format="json").json()["version"]
    r = lead.post(url(open_item, "transition"), {"version": v, "status": "resolved"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "resolved"


def test_handoff_through_api(world, open_item):
    r = client_for(world.lead).post(url(open_item, "handoff"), {
        "version": 1, "team": world.compliance.id, "reason": "KYC issue",
    }, format="json")
    assert r.status_code == 200 and r.json()["team_name"] == "Compliance"


def test_comments_and_activity_timeline(world, open_item):
    alice = client_for(world.alice)
    assert alice.post(url(open_item, "comments"), {"body": "Looking into it"}, format="json").status_code == 201
    assert alice.get(url(open_item, "comments")).json()["results"][0]["body"] == "Looking into it"

    events = alice.get(url(open_item, "activity")).json()["results"]
    assert [e["type"] for e in events] == ["commented", "created"]


def test_activity_is_paginated(world, open_item):
    alice = client_for(world.alice)
    for i in range(60):
        alice.post(url(open_item, "comments"), {"body": f"note {i}"}, format="json")

    first = alice.get(url(open_item, "activity")).json()
    assert len(first["results"]) == 50 and first["next_before"]
    second = alice.get(url(open_item, "activity"), {"before": first["next_before"]}).json()
    assert len(second["results"]) == 11 and second["next_before"] is None
