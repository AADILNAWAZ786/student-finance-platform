"""V-ENR: /enrollments/ endpoints + enrollment lifecycle, validated against OpenAPI."""

from datetime import date

import pytest

from academics import services
from academics.models import Enrollment, EnrollmentEvent
from core.exceptions import StateConflict

pytestmark = pytest.mark.django_db

EL, ED = "/api/v1/enrollments/", "/api/v1/enrollments/{enrollment_id}/"
EV = "/api/v1/enrollments/{enrollment_id}/events/"


def err(r, status, code="VALIDATION_ERROR"):
    assert r.status_code == status, r.content
    assert r.json()["error"]["code"] == code
    return r.json()["error"]["details"]


def enr(**kw):
    b = {
        "student": "STU-000001",
        "term": "TERM-2026-FALL",
        "credits": 16,
        "enrolled_on": "2026-08-20",
    }
    b.update(kw)
    return {k: v for k, v in b.items() if v is not ...}


@pytest.fixture
def api(admin_api, student):
    return admin_api


def test_enroll_valid(api, assert_matches_openapi):
    r = api.post(EL, enr(), format="json")
    assert_matches_openapi(r, EL, "post")
    assert r.status_code == 201
    assert r.json() == {
        "enrollment_id": "ENR-2026-000001",
        "student": "STU-000001",
        "program": "PRG-001",
        "term": "TERM-2026-FALL",
        "credits": 16,
        "status": "ENROLLED",
        "enrolled_on": "2026-08-20",
        "withdrawal_date": None,
        "cancellation_date": None,
    }
    ev = EnrollmentEvent.objects.get()
    assert (ev.from_status, ev.to_status, ev.event_date, ev.reason) == (
        None,
        "ENROLLED",
        date(2026, 8, 20),
        "ENROLLED",
    )


@pytest.mark.parametrize("field", ["student", "term", "credits"])
def test_enroll_missing_required(api, field):
    assert err(api.post(EL, enr(**{field: ...}), format="json"), 400)[field] == [
        "This field is required."
    ]


def test_enrolled_on_defaults_to_today(api, frozen_today):
    frozen_today(date(2026, 9, 1))
    r = api.post(EL, enr(enrolled_on=...), format="json")
    assert r.status_code == 201 and r.json()["enrolled_on"] == "2026-09-01"


def test_default_today_outside_window_rejected(api, frozen_today):
    frozen_today(date(2026, 11, 13))  # after FALL add/drop (14 Sep)
    assert "enrolled_on" in err(api.post(EL, enr(enrolled_on=...), format="json"), 400)


@pytest.mark.parametrize(
    ("credits", "status", "code"),
    [
        (12, 201, None),
        (20, 201, None),
        (16, 201, None),
        (11, 422, "CREDITS_OUT_OF_RANGE"),
        (21, 422, "CREDITS_OUT_OF_RANGE"),
        (24, 422, "CREDITS_OUT_OF_RANGE"),
        (1, 422, "CREDITS_OUT_OF_RANGE"),
        (0, 400, "VALIDATION_ERROR"),
        (25, 400, "VALIDATION_ERROR"),
        (-3, 400, "VALIDATION_ERROR"),
        ("abc", 400, "VALIDATION_ERROR"),
        (16.5, 400, "VALIDATION_ERROR"),
        (None, 400, "VALIDATION_ERROR"),
    ],
)
def test_credit_limits(api, credits, status, code, assert_matches_openapi):
    r = api.post(EL, enr(credits=credits), format="json")
    assert_matches_openapi(r, EL, "post")
    assert r.status_code == status, r.content
    if code:
        assert r.json()["error"]["code"] == code
    if code == "CREDITS_OUT_OF_RANGE":
        assert r.json()["error"]["details"] == {
            "credits": credits,
            "min_credits": 12,
            "max_credits": 20,
            "program_id": "PRG-001",
        }


@pytest.mark.parametrize(
    ("term", "on", "ok"),
    [
        ("TERM-2026-SPRING", "2026-02-02", True),  # start (26 Jan) + 7
        ("TERM-2026-SPRING", "2026-02-03", False),  # start + 8
        ("TERM-2026-SUMMER", "2026-06-01", True),
        ("TERM-2026-SUMMER", "2026-06-02", False),
        ("TERM-2026-FALL", "2026-09-14", True),
        ("TERM-2026-FALL", "2026-09-15", False),
        ("TERM-2026-FALL", "2026-01-01", True),  # early registration is allowed
    ],
)
def test_enrollment_date_restriction(api, term, on, ok):
    r = api.post(EL, enr(term=term, enrolled_on=on), format="json")
    if ok:
        assert r.status_code == 201, r.content
    else:
        assert "start + 7 days" in err(r, 400)["enrolled_on"][0]


@pytest.mark.parametrize("value", ["2026-02-30", "20/08/2026", 20260820, ""])
def test_enrolled_on_invalid_format(api, value):
    assert "enrolled_on" in err(api.post(EL, enr(enrolled_on=value), format="json"), 400)


@pytest.mark.parametrize("term", ["TERM-2023-SPRING", "TERM-2024-SUMMER", "TERM-2025-FALL"])
def test_historical_terms_not_billable_422(api, terms, term, assert_matches_openapi):
    r = api.post(EL, enr(term=term, enrolled_on="2020-01-01"), format="json")
    assert_matches_openapi(r, EL, "post")
    assert err(r, 422, "TERM_NOT_BILLABLE") == {"term_id": term}
    assert Enrollment.objects.count() == 0


def test_one_enrollment_per_student_per_term(api, make_student, assert_matches_openapi):
    assert api.post(EL, enr(), format="json").status_code == 201
    r = api.post(EL, enr(credits=12, enrolled_on="2026-09-01"), format="json")
    assert_matches_openapi(r, EL, "post")
    assert err(r, 409, "ENROLLMENT_EXISTS")["existing_enrollment_id"] == "ENR-2026-000001"
    other = make_student()
    assert api.post(EL, enr(student=other.student_id), format="json").status_code == 201
    assert (
        api.post(
            EL, enr(term="TERM-2026-SUMMER", enrolled_on="2026-05-20"), format="json"
        ).status_code
        == 201
    )
    assert Enrollment.objects.count() == 3 and EnrollmentEvent.objects.count() == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("student", "STU-999999"),
        ("student", "stu-000001"),
        ("term", "TERM-2030-FALL"),
        ("term", "TERM-2026-WINTER"),
        ("student", None),
        ("term", 2026),
    ],
)
def test_invalid_foreign_keys(api, field, value):
    assert field in err(api.post(EL, enr(**{field: value}), format="json"), 400)


@pytest.mark.parametrize(
    "field",
    [
        "program",
        "status",
        "enrollment_id",
        "withdrawal_date",
        "cancellation_date",
        "recorded_at",
        "actor",
    ],
)
def test_server_controlled_fields_rejected(api, field):
    assert err(api.post(EL, enr(**{field: "X"}), format="json"), 400) == {field: ["Unknown field."]}
    assert Enrollment.objects.count() == 0


def test_cannot_enroll_before_admit_term(api, make_student, terms):
    s = make_student(admit_term=terms["TERM-2026-FALL"])
    r = api.post(
        EL,
        enr(student=s.student_id, term="TERM-2026-SUMMER", enrolled_on="2026-05-20"),
        format="json",
    )
    assert "starts before the student's admit term" in err(r, 400)["term"][0]


def test_inactive_program_cannot_enroll(api, student):
    student.program.is_active = False
    student.program.save()
    assert "not active" in err(api.post(EL, enr(), format="json"), 400)["student"][0]


def test_program_is_taken_from_the_student(api, make_student, make_program):
    mba = make_program(code="MBA", min_credits=6, max_credits=10)
    s = make_student(program=mba)
    r = api.post(EL, enr(student=s.student_id, credits=8), format="json")
    assert r.status_code == 201 and r.json()["program"] == mba.program_id


# --- list / detail / events ------------------------------------------------------------


@pytest.fixture
def three(api, make_student):
    b = make_student()
    for body in (
        enr(),
        enr(student=b.student_id, enrolled_on="2026-09-10"),
        enr(term="TERM-2026-SUMMER", enrolled_on="2026-05-20"),
    ):
        assert api.post(EL, body, format="json").status_code == 201
    return api


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, ["ENR-2026-000001", "ENR-2026-000002", "ENR-2026-000003"]),
        ({"student": "STU-000001"}, ["ENR-2026-000001", "ENR-2026-000003"]),
        ({"term": "TERM-2026-SUMMER"}, ["ENR-2026-000003"]),
        ({"program": "PRG-001", "term": "TERM-2026-FALL"}, ["ENR-2026-000001", "ENR-2026-000002"]),
        ({"status": "ENROLLED"}, ["ENR-2026-000001", "ENR-2026-000002", "ENR-2026-000003"]),
        ({"status": "COMPLETED"}, []),
        ({"enrolled_from": "2026-08-21"}, ["ENR-2026-000002"]),
        ({"enrolled_to": "2026-08-20"}, ["ENR-2026-000001", "ENR-2026-000003"]),
        ({"enrolled_from": "2026-06-01", "enrolled_to": "2026-09-01"}, ["ENR-2026-000001"]),
        ({"ordering": "-enrolled_on"}, ["ENR-2026-000002", "ENR-2026-000001", "ENR-2026-000003"]),
        ({"page_size": 2, "page": 2}, ["ENR-2026-000003"]),
    ],
)
def test_list_filters_ordering_pagination(three, params, expected, assert_matches_openapi):
    r = three.get(EL, params)
    assert_matches_openapi(r, EL, "get")
    assert [e["enrollment_id"] for e in r.json()["results"]] == expected


@pytest.mark.parametrize(
    ("params", "status"),
    [({"status": "PAUSED"}, 400), ({"enrolled_from": "not-a-date"}, 400), ({"page": 50}, 404)],
)
def test_list_errors(three, params, status, assert_matches_openapi):
    r = three.get(EL, params)
    assert_matches_openapi(r, EL, "get")
    assert r.status_code == status


def test_detail_and_events(three, assert_matches_openapi):
    r = three.get(ED.format(enrollment_id="ENR-2026-000002"))
    assert_matches_openapi(r, ED, "get")
    assert r.json()["student"] == "STU-000002"
    r = three.get(EV.format(enrollment_id="ENR-2026-000002"))
    assert_matches_openapi(r, EV, "get")
    assert [(e["from_status"], e["to_status"]) for e in r.json()] == [(None, "ENROLLED")]
    for path in (ED, EV):
        r = three.get(path.format(enrollment_id="ENR-2026-999999"))
        assert_matches_openapi(r, path, "get")
        err(r, 404, "NOT_FOUND")


# --- enrollment lifecycle (only COMPLETED is reachable in Phase 1) ---------------------


def test_completion_after_term_end_writes_event(three, terms, assert_matches_openapi):
    e = Enrollment.objects.get(pk="ENR-2026-000003")  # SUMMER ends 15 Aug
    services.complete_enrollment(e, on_date=date(2026, 8, 16), actor="system")
    r = three.get(EV.format(enrollment_id=e.pk))
    assert_matches_openapi(r, EV, "get")
    assert [(x["from_status"], x["to_status"], x["event_date"]) for x in r.json()] == [
        (None, "ENROLLED", "2026-05-20"),
        ("ENROLLED", "COMPLETED", "2026-08-16"),
    ]
    assert three.get(ED.format(enrollment_id=e.pk)).json()["status"] == "COMPLETED"


def test_completion_on_or_before_end_rejected(three):
    e = Enrollment.objects.get(pk="ENR-2026-000003")
    with pytest.raises(Exception, match="only after the term ends"):
        services.complete_enrollment(e, on_date=date(2026, 8, 15), actor="system")
    assert e.events.count() == 1


@pytest.mark.parametrize("terminal", ["COMPLETED", "WITHDRAWN", "CANCELLED"])
def test_terminal_enrollment_states_reject_completion(three, terminal):
    e = Enrollment.objects.get(pk="ENR-2026-000003")
    Enrollment.objects.filter(pk=e.pk).update(
        status=terminal,
        withdrawal_date=date(2026, 6, 1) if terminal == "WITHDRAWN" else None,
        cancellation_date=date(2026, 5, 1) if terminal == "CANCELLED" else None,
    )
    with pytest.raises(StateConflict) as exc:
        services.complete_enrollment(e, on_date=date(2026, 9, 1), actor="system")
    assert exc.value.code == "ILLEGAL_TRANSITION"
    assert exc.value.details["from_status"] == terminal
