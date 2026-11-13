"""core.permissions: role groups for the brief's actors (ARCHITECTURE.md §13)."""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import AnonymousUser, Group, User

from core.permissions import ALL_ROLES, PLATFORM_ADMIN, SUPPORT_STAFF, roles_required

pytestmark = pytest.mark.phase0


def test_five_actor_roles():
    assert ALL_ROLES == (
        "learner",
        "instructor",
        "academic_admin",
        "support_staff",
        "platform_admin",
    )


def test_unknown_or_empty_roles_rejected():
    with pytest.raises(ValueError):
        roles_required("finance_wizard")
    with pytest.raises(ValueError):
        roles_required()


@pytest.mark.django_db
def test_role_membership_decides():
    perm = roles_required(PLATFORM_ADMIN)()
    admin = User.objects.create_user("admin1")
    admin.groups.add(Group.objects.create(name=PLATFORM_ADMIN))
    staff = User.objects.create_user("staff1")
    staff.groups.add(Group.objects.create(name=SUPPORT_STAFF))
    root = User.objects.create_superuser("root", password="x")

    def allowed(user):
        return perm.has_permission(SimpleNamespace(user=user), None)

    assert allowed(admin) is True
    assert allowed(staff) is False
    assert allowed(root) is True
    assert allowed(AnonymousUser()) is False
