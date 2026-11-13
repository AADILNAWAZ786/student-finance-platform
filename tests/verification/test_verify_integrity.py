"""V-INT: transaction rollback, DB constraints behind the API, and deterministic results."""

from datetime import date
from decimal import Decimal
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from academics import services as academics_services
from academics.models import Enrollment, EnrollmentEvent
from core.models import IdSequence
from students import services as student_services
from students.models import Student, StudentStatusEvent

pytestmark = pytest.mark.django_db


def _seq(key):
    row = IdSequence.objects.filter(key=key).first()
    return row.last_value if row else 0


# --- rollback: a failure after the main row is written leaves nothing behind -----------


def test_student_create_rolls_back_when_event_write_fails(terms, program):
    with mock.patch("students.services._status_event", side_effect=RuntimeError("disk")):
        with pytest.raises(RuntimeError):
            student_services.create_student(
                first_name="A",
                last_name="B",
                email="a@example.edu",
                date_of_birth=date(2006, 1, 1),
                residency="DOMESTIC",
                program=program,
                admit_term=terms["TERM-2026-SPRING"],
                gpa=Decimal("3.00"),
                need_index=Decimal("0.10"),
                event_date=date(2026, 1, 5),
                actor="t",
            )
    assert Student.objects.count() == 0 and _seq("STU") == 0


def test_enroll_rolls_back_when_event_write_fails(student, terms):
    with mock.patch("academics.services._event", side_effect=RuntimeError("disk")):
        with pytest.raises(RuntimeError):
            academics_services.enroll(
                student=student,
                term=terms["TERM-2026-FALL"],
                credits=16,
                enrolled_on=date(2026, 8, 20),
                actor="t",
            )
    assert Enrollment.objects.count() == 0 and _seq("ENR:2026") == 0


def test_status_change_rolls_back_when_event_write_fails(student):
    with mock.patch("students.services._status_event", side_effect=RuntimeError("disk")):
        with pytest.raises(RuntimeError):
            student_services.change_status(
                student, to_status="ON_HOLD", event_date=date(2026, 2, 1), reason="X", actor="t"
            )
    student.refresh_from_db()
    assert student.status == "ACTIVE" and student.status_events.count() == 1


def test_api_500_on_unexpected_error_leaves_no_partial_rows(admin_api, student):
    admin_api.raise_request_exception = False
    with mock.patch("academics.services._event", side_effect=RuntimeError("disk")):
        r = admin_api.post(
            "/api/v1/enrollments/",
            {
                "student": student.student_id,
                "term": "TERM-2026-FALL",
                "credits": 16,
                "enrolled_on": "2026-08-20",
            },
            format="json",
        )
    assert r.status_code == 500
    assert Enrollment.objects.count() == 0 and EnrollmentEvent.objects.count() == 0


def test_rejected_requests_consume_no_ids(admin_api, student):
    for credits in (30, 21):  # 400, then 422
        admin_api.post(
            "/api/v1/enrollments/",
            {
                "student": student.student_id,
                "term": "TERM-2026-FALL",
                "credits": credits,
                "enrolled_on": "2026-08-20",
            },
            format="json",
        )
    r = admin_api.post(
        "/api/v1/enrollments/",
        {
            "student": student.student_id,
            "term": "TERM-2026-FALL",
            "credits": 16,
            "enrolled_on": "2026-08-20",
        },
        format="json",
    )
    assert r.json()["enrollment_id"] == "ENR-2026-000001"


# --- constraints hold even if application code is bypassed ----------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"status": "SUSPENDED"},
        {"gpa": Decimal("5.00")},
        {"need_index": Decimal("2.00")},
        {"email": ""},
        {"residency": "MARS"},
    ],
)
def test_student_constraints_block_raw_updates(student, change):
    with pytest.raises(IntegrityError), transaction.atomic():
        Student.objects.filter(pk=student.pk).update(**change)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "WITHDRAWN"},  # no withdrawal_date
        {"cancellation_date": date(2026, 9, 1)},  # date without CANCELLED
        {"status": "BOGUS"},
        {"credits": 0},
    ],
)
def test_enrollment_constraints_block_raw_updates(student, terms, change):
    e = academics_services.enroll(
        student=student,
        term=terms["TERM-2026-FALL"],
        credits=16,
        enrolled_on=date(2026, 8, 20),
        actor="t",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Enrollment.objects.filter(pk=e.pk).update(**change)


def test_duplicate_primary_keys_rejected(student):
    with pytest.raises(IntegrityError), transaction.atomic():
        Student.objects.create(
            **{
                f.attname: getattr(student, f.attname)
                for f in Student._meta.concrete_fields
                if f.attname != "email"
            },
            email="other@example.edu",
        )


def test_status_events_reference_existing_students_only():
    """SQLite FKs are DEFERRABLE INITIALLY DEFERRED in Django: checked at commit time."""
    from django.db import connection

    with pytest.raises(IntegrityError), transaction.atomic():
        StudentStatusEvent.objects.create(
            event_id="EVT-STU-2026-9999999",
            student_id="STU-404404",
            to_status="ACTIVE",
            event_date=date(2026, 1, 1),
            reason="X",
            actor="t",
        )
        connection.check_constraints()  # what COMMIT does


# --- determinism ---------------------------------------------------------------------


def _scenario(api):
    out = []
    out.append(
        api.post(
            "/api/v1/programs/",
            {
                "code": "DET",
                "name": "D",
                "level": "UG",
                "department": "D",
                "is_stem": True,
                "has_lab": False,
                "min_credits": 12,
                "max_credits": 20,
            },
            format="json",
        ).json()
    )
    for i in range(3):
        out.append(
            api.post(
                "/api/v1/students/",
                {
                    "first_name": f"F{i}",
                    "last_name": "L",
                    "email": f"det{i}@example.edu",
                    "date_of_birth": "2005-05-05",
                    "residency": "DOMESTIC",
                    "program": "PRG-001",
                    "admit_term": "TERM-2026-SPRING",
                    "gpa": "3.10",
                    "need_index": "0.40",
                },
                format="json",
            ).json()
        )
        out.append(
            api.post(
                "/api/v1/enrollments/",
                {
                    "student": f"STU-00000{i + 1}",
                    "term": "TERM-2026-FALL",
                    "credits": 14,
                    "enrolled_on": "2026-08-25",
                },
                format="json",
            ).json()
        )
    out.append(
        api.post(
            "/api/v1/students/STU-000002/status/",
            {"to_status": "ON_HOLD", "reason": "X", "event_date": "2026-09-01"},
            format="json",
        ).json()["event"]
        | {"actor": None}
    )
    out.append(api.get("/api/v1/enrollments/", {"ordering": "-enrollment_id"}).json())
    return out


@pytest.mark.django_db(transaction=True)
def test_same_requests_give_identical_results(api_client_for, frozen_today):
    frozen_today(date(2026, 1, 5))
    results = []
    for _ in range(2):
        call_command("flush", interactive=False, verbosity=0)
        call_command("seed_terms", verbosity=0)
        results.append(_scenario(api_client_for("academic_admin")))
    assert results[0] == results[1]
    assert results[0][1]["student_id"] == "STU-000001"
    assert results[0][-2]["event_id"] == "EVT-STU-2026-0000004"  # 3 creations + 1 change
