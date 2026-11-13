"""seed_terms and seed_roles management commands."""

from io import StringIO

import pytest
from django.contrib.auth.models import Group, User
from django.core.management import CommandError, call_command
from rest_framework.authtoken.models import Token

from academics.models import Term

pytestmark = pytest.mark.django_db


def test_seed_terms_idempotent_and_strict():
    out = StringIO()
    call_command("seed_terms", stdout=out)
    assert "created: 12" in out.getvalue()
    assert Term.objects.filter(is_billable=True).count() == 3
    assert Term.objects.filter(is_billable=False).count() == 9
    out = StringIO()
    call_command("seed_terms", "--strict", stdout=out)
    assert "created: 0, already present: 12" in out.getvalue()


def test_seed_terms_strict_reports_drift():
    call_command("seed_terms", verbosity=0)
    Term.objects.filter(pk="TERM-2026-FALL").update(late_fee_grace_days=10)
    with pytest.raises(CommandError):
        call_command("seed_terms", "--strict", stdout=StringIO())


def test_seed_roles_groups_and_demo_users_without_passwords():
    out = StringIO()
    call_command("seed_roles", "--demo-users", stdout=out)
    assert set(Group.objects.values_list("name", flat=True)) == {
        "learner",
        "instructor",
        "academic_admin",
        "support_staff",
        "platform_admin",
    }
    user = User.objects.get(username="demo_academic_admin")
    assert not user.has_usable_password()
    assert Token.objects.get(user=user).key in out.getvalue()
    call_command("seed_roles", "--demo-users", stdout=StringIO())  # idempotent
    assert User.objects.filter(username__startswith="demo_").count() == 5
