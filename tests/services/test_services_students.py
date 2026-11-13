"""students.services: creation rules, contact updates, status lifecycle + events."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from core.exceptions import StateConflict
from core.models import IdSequence
from students import services
from students.calculators import age_on
from students.lifecycle import STUDENT_TRANSITIONS
from students.models import Student, StudentStatusEvent

pytestmark = pytest.mark.django_db


def test_create_assigns_sequential_ids_and_first_event(make_student):
    a, b = make_student(), make_student()
    assert (a.student_id, b.student_id) == ("STU-000001", "STU-000002")
    ev = a.status_events.get()
    assert (ev.event_id, ev.from_status, ev.to_status, ev.reason, ev.event_date) == (
        "EVT-STU-2026-0000001",
        None,
        "ACTIVE",
        "CREATED",
        date(2026, 1, 5),
    )
    assert a.status == "ACTIVE"


def test_email_domain_case_normalised_and_unique(make_student):
    s = make_student(email="Asha.Rao@EXAMPLE.EDU")
    assert s.email == "Asha.Rao@example.edu"
    with pytest.raises(ValidationError) as exc:
        make_student(email="asha.rao@example.edu")  # case-insensitive clash
    assert "already exists" in str(exc.value.message_dict["email"])


def test_email_must_use_synthetic_domain(make_student):
    with pytest.raises(ValidationError) as exc:
        make_student(email="someone@gmail.com")
    assert "example.edu" in exc.value.message_dict["email"][0]


@pytest.mark.parametrize(
    ("dob", "ok"),
    [
        (date(2010, 1, 26), True),  # exactly 16 on 26 Jan 2026 (SPRING start)
        (date(2010, 1, 27), False),  # 15
        (date(1965, 1, 26), False),  # 61
        (date(1966, 1, 27), True),  # 59 → within 16–60
    ],
)
def test_age_16_to_60_on_admit_term_start(make_student, dob, ok):
    if ok:
        make_student(date_of_birth=dob)
    else:
        with pytest.raises(ValidationError) as exc:
            make_student(date_of_birth=dob)
        assert "date_of_birth" in exc.value.message_dict


def test_age_on_leap_day():
    assert age_on(date(2008, 2, 29), date(2026, 2, 28)) == 17
    assert age_on(date(2008, 2, 29), date(2026, 3, 1)) == 18


def test_inactive_program_rejected(make_student, make_program):
    with pytest.raises(ValidationError) as exc:
        make_student(program=make_program(code="OLD-1", is_active=False))
    assert "program" in exc.value.message_dict


def test_failed_create_leaves_nothing_behind(make_student):
    make_student()
    with pytest.raises(ValidationError):
        make_student(email="x@gmail.com")
    assert Student.objects.count() == 1
    assert StudentStatusEvent.objects.count() == 1
    assert IdSequence.objects.get(key="STU").last_value == 1


def test_update_contact_only(student):
    services.update_contact(student, first_name=" Asha ", email="new@example.edu")
    student.refresh_from_db()
    assert (student.first_name, student.email) == ("Asha", "new@example.edu")
    with pytest.raises(ValidationError) as exc:
        services.update_contact(student, gpa=Decimal("4.00"))
    assert "gpa" in exc.value.message_dict


def test_update_contact_email_clash(make_student):
    a, b = make_student(), make_student()
    with pytest.raises(ValidationError):
        services.update_contact(b, email=a.email)


@pytest.mark.parametrize(
    "path",
    [
        ["ON_HOLD"],
        ["ON_HOLD", "ACTIVE"],
        ["WITHDRAWN"],
        ["GRADUATED"],
        ["ON_HOLD", "ACTIVE", "GRADUATED"],
    ],
)
def test_allowed_status_paths_write_one_event_each(student, path):
    for i, to in enumerate(path):
        s, ev = services.change_status(
            student, to_status=to, event_date=date(2026, 2, 1 + i), reason="TEST", actor="t"
        )
        assert s.status == to and ev.to_status == to
    events = list(
        student.status_events.order_by("event_id").values_list("from_status", "to_status")
    )
    assert events == list(zip([None, "ACTIVE", *path[:-1]], ["ACTIVE", *path], strict=True))


@pytest.mark.parametrize(
    ("setup", "target"),
    [
        ([], "ACTIVE"),
        (["GRADUATED"], "ACTIVE"),
        (["WITHDRAWN"], "ON_HOLD"),
        (["ON_HOLD"], "GRADUATED"),
        (["ON_HOLD"], "WITHDRAWN"),
    ],
)
def test_illegal_transitions_raise_409_and_write_nothing(student, setup, target):
    for to in setup:
        services.change_status(
            student, to_status=to, event_date=date(2026, 2, 1), reason="T", actor="t"
        )
    before = student.status_events.count()
    with pytest.raises(StateConflict) as exc:
        services.change_status(
            student, to_status=target, event_date=date(2026, 3, 1), reason="T", actor="t"
        )
    assert exc.value.code == "ILLEGAL_TRANSITION" and exc.value.status_code == 409
    assert student.status_events.count() == before


def test_event_date_cannot_go_backwards(student):
    with pytest.raises(ValidationError):
        services.change_status(
            student, to_status="ON_HOLD", event_date=date(2026, 1, 4), reason="T", actor="t"
        )


def test_transition_table_matches_design():
    assert {k: set(v) for k, v in STUDENT_TRANSITIONS.items()} == {
        None: {"ACTIVE"},
        "ACTIVE": {"ON_HOLD", "WITHDRAWN", "GRADUATED"},
        "ON_HOLD": {"ACTIVE"},
        "WITHDRAWN": set(),
        "GRADUATED": set(),
    }
