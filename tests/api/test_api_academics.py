"""Programs, terms and enrollments API (ARCHITECTURE §9.2)."""

import pytest

pytestmark = pytest.mark.django_db

PROGRAM = {
    "code": "BSC-CS",
    "name": "BSc Computer Science",
    "level": "UG",
    "department": "Computing",
    "is_stem": True,
    "has_lab": True,
    "min_credits": 12,
    "max_credits": 20,
    "is_active": True,
}


# --- programs ----------------------------------------------------------------------


def test_program_create_list_filter(admin_api, staff_api, api_client_for):
    r = admin_api.post("/api/v1/programs/", PROGRAM, format="json")
    assert r.status_code == 201 and r.json() == {"program_id": "PRG-001", **PROGRAM}
    admin_api.post(
        "/api/v1/programs/",
        {**PROGRAM, "code": "MBA", "level": "PG", "is_stem": False},
        format="json",
    )
    ids = lambda r: [p["program_id"] for p in r.json()["results"]]  # noqa: E731
    assert ids(staff_api.get("/api/v1/programs/", {"level": "PG"})) == ["PRG-002"]
    assert ids(staff_api.get("/api/v1/programs/", {"is_stem": "true"})) == ["PRG-001"]
    assert api_client_for("instructor").get("/api/v1/programs/").status_code == 200
    assert staff_api.get("/api/v1/programs/PRG-002/").json()["code"] == "MBA"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"min_credits": 20, "max_credits": 12}, "max_credits"),
        ({"min_credits": 4}, "max_credits"),
        ({"level": "PHD"}, "level"),
        ({"program_id": "PRG-777"}, "program_id"),
    ],
)
def test_program_validation_400(admin_api, change, field):
    r = admin_api.post("/api/v1/programs/", {**PROGRAM, **change}, format="json")
    assert r.status_code == 400 and field in r.json()["error"]["details"]


def test_program_duplicate_code_400_and_write_roles(admin_api, staff_api):
    admin_api.post("/api/v1/programs/", PROGRAM, format="json")
    r = admin_api.post("/api/v1/programs/", PROGRAM, format="json")
    assert r.status_code == 400 and "code" in r.json()["error"]["details"]
    assert (
        staff_api.post("/api/v1/programs/", {**PROGRAM, "code": "X"}, format="json").status_code
        == 403
    )


# --- terms -------------------------------------------------------------------------

TERM_2027 = {
    "year": 2027,
    "season": "SPRING",
    "is_billable": False,
    "invoice_date": "2027-01-04",
    "due_date": "2027-02-01",
    "start_date": "2027-01-25",
    "census_date": "2027-02-22",
    "end_date": "2027-05-08",
}


def test_term_list_filter_and_create(admin_api, terms):
    r = admin_api.get("/api/v1/terms/", {"is_billable": "true"})
    assert [t["term_id"] for t in r.json()["results"]] == [
        "TERM-2026-SPRING",
        "TERM-2026-SUMMER",
        "TERM-2026-FALL",
    ]
    assert admin_api.get("/api/v1/terms/", {"year": 2024}).json()["count"] == 3
    fall = admin_api.get("/api/v1/terms/TERM-2026-FALL/").json()
    assert fall == {
        "term_id": "TERM-2026-FALL",
        "year": 2026,
        "season": "FALL",
        "is_billable": True,
        "invoice_date": "2026-08-17",
        "due_date": "2026-09-14",
        "start_date": "2026-09-07",
        "census_date": "2026-10-05",
        "end_date": "2026-12-19",
        "late_fee_grace_days": 14,
    }
    r = admin_api.post("/api/v1/terms/", TERM_2027, format="json")
    assert r.status_code == 201 and r.json()["term_id"] == "TERM-2027-SPRING"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"census_date": "2027-02-23"}, "census_date"),  # not start + 28
        ({"due_date": "2027-01-03"}, "due_date"),  # before invoice
        ({"season": "WINTER"}, "season"),
        ({"year": 1999}, "year"),
    ],
)
def test_term_validation_400(admin_api, terms, change, field):
    r = admin_api.post("/api/v1/terms/", {**TERM_2027, **change}, format="json")
    assert r.status_code == 400 and field in r.json()["error"]["details"]


def test_duplicate_term_400(admin_api, terms):
    r = admin_api.post(
        "/api/v1/terms/",
        {
            **TERM_2027,
            "year": 2026,
            "season": "FALL",
            "start_date": "2026-09-07",
            "census_date": "2026-10-05",
            "end_date": "2026-12-19",
            "invoice_date": "2026-08-17",
            "due_date": "2026-09-14",
        },
        format="json",
    )
    assert r.status_code == 400 and "already exists" in str(r.json()["error"]["details"])


# --- enrollments -------------------------------------------------------------------


def _enroll(api, **kw):
    body = {
        "student": "STU-000001",
        "term": "TERM-2026-FALL",
        "credits": 16,
        "enrolled_on": "2026-08-20",
    }
    body.update(kw)
    return api.post("/api/v1/enrollments/", body, format="json")


def test_enroll_201_and_events(admin_api, student):
    r = _enroll(admin_api)
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
    ev = admin_api.get("/api/v1/enrollments/ENR-2026-000001/events/").json()
    assert [(e["from_status"], e["to_status"], e["event_date"]) for e in ev] == [
        (None, "ENROLLED", "2026-08-20")
    ]


def test_enroll_errors(admin_api, student):
    assert _enroll(admin_api).status_code == 201
    dup = _enroll(admin_api)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "ENROLLMENT_EXISTS"
    r = _enroll(admin_api, term="TERM-2025-FALL", enrolled_on="2025-09-01")
    assert r.status_code == 422 and r.json()["error"]["code"] == "TERM_NOT_BILLABLE"
    r = _enroll(admin_api, term="TERM-2026-SUMMER", credits=25, enrolled_on="2026-05-20")
    assert r.status_code == 400 and "credits" in r.json()["error"]["details"]  # > 24 shape
    r = _enroll(admin_api, term="TERM-2026-SUMMER", credits=21, enrolled_on="2026-05-20")
    assert r.status_code == 422 and r.json()["error"]["code"] == "CREDITS_OUT_OF_RANGE"
    r = _enroll(admin_api, term="TERM-2026-SPRING", enrolled_on="2026-02-03")
    assert r.status_code == 400 and "enrolled_on" in r.json()["error"]["details"]
    r = _enroll(admin_api, student="STU-999999", term="TERM-2026-SPRING")
    assert r.status_code == 400 and "student" in r.json()["error"]["details"]
    r = _enroll(admin_api, term="TERM-2026-SPRING", credits="16", program="PRG-001")
    assert r.status_code == 400 and "program" in r.json()["error"]["details"]


def test_enrollment_filters_and_roles(admin_api, staff_api, api_client_for, make_student):
    a, b = make_student(), make_student()
    _enroll(admin_api, student=a.student_id)
    _enroll(admin_api, student=b.student_id)
    _enroll(admin_api, student=a.student_id, term="TERM-2026-SUMMER", enrolled_on="2026-05-20")
    ids = lambda r: [e["enrollment_id"] for e in r.json()["results"]]  # noqa: E731
    assert ids(staff_api.get("/api/v1/enrollments/", {"student": a.student_id})) == [
        "ENR-2026-000001",
        "ENR-2026-000003",
    ]
    assert ids(staff_api.get("/api/v1/enrollments/", {"term": "TERM-2026-SUMMER"})) == [
        "ENR-2026-000003"
    ]
    assert staff_api.get("/api/v1/enrollments/", {"status": "ENROLLED"}).json()["count"] == 3
    assert ids(staff_api.get("/api/v1/enrollments/", {"enrolled_from": "2026-08-01"})) == [
        "ENR-2026-000001",
        "ENR-2026-000002",
    ]
    assert staff_api.get("/api/v1/enrollments/", {"status": "PAUSED"}).status_code == 400
    assert _enroll(staff_api, student=b.student_id, term="TERM-2026-SPRING").status_code == 403
    assert api_client_for("instructor").get("/api/v1/enrollments/").status_code == 403
    r = staff_api.get("/api/v1/enrollments/ENR-2026-999999/")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
