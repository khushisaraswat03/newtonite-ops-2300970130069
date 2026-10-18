import pytest
from django.contrib.auth.models import User

from ops.models import Membership, Team
from ops.services import create_item


@pytest.fixture(autouse=True)
def fast_passwords(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def world(db):
    payments = Team.objects.create(name="Payments")
    compliance = Team.objects.create(name="Compliance")

    def user(name, roles, admin=False):
        u = User.objects.create_user(username=name, password="x", is_superuser=admin)
        for team, role in roles.items():
            Membership.objects.create(user=u, team=team, role=role)
        return u

    class World:
        pass

    w = World()
    w.payments, w.compliance = payments, compliance
    w.lead = user("lead", {payments: "lead"})
    w.lead2 = user("lead2", {payments: "lead"})
    w.alice = user("alice", {payments: "member"})
    w.bob = user("bob", {payments: "member"})
    w.viewer = user("viewer", {payments: "viewer"})
    w.outsider = user("outsider", {compliance: "member"})
    w.admin = user("admin", {}, admin=True)
    return w


@pytest.fixture
def open_item(world):
    return create_item(
        world.alice, team=world.payments, title="Refund stuck", type="payment", priority="P2",
    )
