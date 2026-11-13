"""Django Admin: pages load; add/delete disabled (services own creation; history kept)."""

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import Client

from academics.models import Enrollment, Program, Term
from students.models import Student

pytestmark = pytest.mark.django_db


@pytest.fixture
def site_client(db):
    c = Client()
    c.force_login(User.objects.create_superuser("root"))
    return c


def test_changelists_and_change_pages_load(site_client, student, terms):
    from academics import services

    services.enroll(
        student=student,
        term=terms["TERM-2026-FALL"],
        credits=16,
        enrolled_on=terms["TERM-2026-FALL"].start_date,
        actor="t",
    )
    for url in [
        "/admin/students/student/",
        f"/admin/students/student/{student.pk}/change/",
        "/admin/academics/program/",
        "/admin/academics/term/",
        "/admin/academics/term/TERM-2026-FALL/change/",
        "/admin/academics/enrollment/",
        "/admin/academics/enrollment/ENR-2026-000001/change/",
        "/admin/students/student/?q=Rao",
        "/admin/students/student/?status__exact=ACTIVE",
    ]:
        assert site_client.get(url).status_code == 200, url


@pytest.mark.parametrize("model", [Student, Program, Term, Enrollment])
def test_add_and_delete_disabled(rf, model):
    request = rf.get("/")
    request.user = User(is_superuser=True, is_staff=True)
    model_admin = admin.site._registry[model]
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_rule_driving_fields_are_read_only(rf, student):
    request = rf.get("/")
    request.user = User(is_superuser=True, is_staff=True)
    ro = set(admin.site._registry[Student].get_readonly_fields(request, student))
    assert {"student_id", "status", "gpa", "program", "admit_term"} <= ro
    assert not {"first_name", "last_name", "email"} & ro  # contact fields stay editable
