import pytest

from ops import services
from ops.models import ActivityEvent, WorkItem


def test_item_is_not_saved_if_writing_its_history_fails(world, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash while writing history")

    monkeypatch.setattr(services, "_log", crash)
    with pytest.raises(RuntimeError):
        services.create_item(world.alice, team=world.payments, title="Ghost", type="task", priority="P3")

    assert not WorkItem.objects.filter(title="Ghost").exists()
    assert not ActivityEvent.objects.exists()


def test_every_change_has_a_matching_event(world, open_item):
    item = services.claim_item(world.alice, open_item)
    services.update_item(world.alice, item, version=item.version, priority="P1")
    types = list(ActivityEvent.objects.filter(item=open_item).order_by("id").values_list("type", flat=True))
    assert types == ["created", "claimed", "status_changed", "priority_changed"]
