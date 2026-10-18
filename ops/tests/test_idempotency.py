import threading

import pytest
from django.db import connection
from rest_framework.test import APIClient

from ops.models import IdempotencyKey, WorkItem

NEW_ITEM = {"title": "Refund not credited", "type": "payment", "priority": "P2"}


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def create(client, world, key=None, **overrides):
    body = {**NEW_ITEM, "team": world.payments.id, **overrides}
    headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    return client.post("/api/items/", body, format="json", **headers)


def test_same_key_twice_creates_one_item_and_returns_same_response(world):
    alice = client_for(world.alice)
    first = create(alice, world, key="click-123")
    second = create(alice, world, key="click-123")

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second["Idempotent-Replayed"] == "true"
    assert WorkItem.objects.count() == 1


def test_without_a_key_each_request_runs(world):
    alice = client_for(world.alice)
    create(alice, world)
    create(alice, world)
    assert WorkItem.objects.count() == 2


def test_same_key_with_different_body_is_rejected(world):
    alice = client_for(world.alice)
    create(alice, world, key="click-123")
    r = create(alice, world, key="click-123", title="Something else")
    assert r.status_code == 422
    assert WorkItem.objects.count() == 1


def test_keys_are_per_user(world):
    create(client_for(world.alice), world, key="same-key")
    r = create(client_for(world.bob), world, key="same-key")
    assert r.status_code == 201 and "Idempotent-Replayed" not in r
    assert WorkItem.objects.count() == 2


def test_failed_request_is_not_remembered(world):
    alice = client_for(world.alice)
    assert create(alice, world, key="k1", priority="P9").status_code == 400
    assert not IdempotencyKey.objects.exists()
    assert create(alice, world, key="k1").status_code == 201


def test_double_clicked_claim_returns_success_not_conflict(world, open_item):
    alice = client_for(world.alice)
    headers = {"HTTP_IDEMPOTENCY_KEY": "claim-click-1"}
    first = alice.post(f"/api/items/{open_item.id}/claim/", **headers)
    second = alice.post(f"/api/items/{open_item.id}/claim/", **headers)
    assert first.status_code == second.status_code == 200
    assert second["Idempotent-Replayed"] == "true"


@pytest.mark.django_db(transaction=True)
def test_simultaneous_duplicates_create_one_item(world):
    copies = 5
    barrier = threading.Barrier(copies)
    statuses = []

    def send():
        try:
            barrier.wait()
            statuses.append(create(client_for(world.alice), world, key="retry-storm").status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=send) for _ in range(copies)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses == [201] * copies
    assert WorkItem.objects.count() == 1
