"""Students API (ARCHITECTURE §9.2): requests, validation, errors, filtering, permissions."""

import pytest

pytestmark = pytest.mark.django_db

URL = "/api/v1/students/"


def _payload(**kw):
    p = {
        "first_name": "Asha",
        "last_name": "Rao",
        "email": "stu000001@example.edu",
        "date_of_birth": "2006-03-14",
        "residency": "DOMESTIC",
        "program": "PRG-001",
        "admit_term": "TERM-2026-SPRING",
        "gpa": "3.72",
        "need_index": "0.35",
    }
    p.update(kw)
    return p


@pytest.fixture
def setup(terms, program):
    return program


def test_create_returns_201_with_documented_shape(admin_api, setup):
    r = admin_api.post(URL, _payload(), format="json")
    assert r.status_code == 201, r.json()
    assert r.json() == {
        "student_id": "STU-000001",
        "first_name": "Asha",
        "last_name": "Rao",
        "email": "stu000001@example.edu",
        "date_of_birth": "2006-03-14",
        "residency": "DOMESTIC",
        "program": "PRG-001",
        "admit_term": "TERM-2026-SPRING",
        "gpa": "3.72",
        "need_index": "0.35",
        "status": "ACTIVE",
    }


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("gpa", 3.72, "string"),  # JSON number refused: no floats
        ("gpa", "4.01", "less than or equal"),
        ("gpa", "3.725", "digits"),  # 3 dp rejected (max 3 digits)
        ("need_index", "1.50", "less than or equal"),
        ("residency", "RESIDENT", "valid choice"),
        ("program", "PRG-999", "does not exist"),
        ("email", "not-an-email", "valid email"),
        ("date_of_birth", "14-03-2006", "format"),
    ],
)
def test_create_field_validation_400(admin_api, setup, field, value, fragment):
    r = admin_api.post(URL, _payload(**{field: value}), format="json")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert fragment in " ".join(err["details"][field]).lower() or fragment in str(
        err["details"][field]
    )


def test_create_business_rules_400(admin_api, setup):
    r = admin_api.post(
        URL, _payload(email="x@gmail.com", date_of_birth="2015-01-01"), format="json"
    )
    assert r.status_code == 400
    assert set(r.json()["error"]["details"]) == {"email", "date_of_birth"}


def test_create_rejects_unknown_and_server_owned_fields(admin_api, setup):
    r = admin_api.post(URL, _payload(student_id="STU-999999", status="GRADUATED"), format="json")
    assert r.status_code == 400
    assert set(r.json()["error"]["details"]) == {"student_id", "status"}


def test_duplicate_email_400(admin_api, setup):
    assert admin_api.post(URL, _payload(), format="json").status_code == 201
    r = admin_api.post(URL, _payload(), format="json")
    assert r.status_code == 400 and "email" in r.json()["error"]["details"]


def test_retrieve_and_404_envelope(admin_api, student):
    assert admin_api.get(f"{URL}{student.student_id}/").json()["student_id"] == "STU-000001"
    r = admin_api.get(f"{URL}STU-999999/")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_patch_contact_fields(admin_api, student):
    r = admin_api.patch(
        f"{URL}{student.student_id}/",
        {"last_name": "Rao-Iyer", "email": "asha.ri@example.edu"},
        format="json",
    )
    assert r.status_code == 200
    assert (r.json()["last_name"], r.json()["email"]) == ("Rao-Iyer", "asha.ri@example.edu")


@pytest.mark.parametrize(
    "body", [{"gpa": "4.00"}, {"status": "GRADUATED"}, {"program": "PRG-002"}, {}]
)
def test_patch_non_contact_or_empty_400(admin_api, student, body):
    r = admin_api.patch(f"{URL}{student.student_id}/", body, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("method", ["put", "delete"])
def test_put_and_delete_not_allowed(admin_api, student, method):
    r = getattr(admin_api, method)(f"{URL}{student.student_id}/", {}, format="json")
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_status_change_and_illegal_transition(admin_api, student):
    url = f"{URL}{student.student_id}/status/"
    r = admin_api.post(
        url,
        {"to_status": "ON_HOLD", "reason": "FINANCIAL_HOLD", "event_date": "2026-09-20"},
        format="json",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["student"]["status"] == "ON_HOLD"
    assert body["event"] == {
        "event_id": "EVT-STU-2026-0000002",
        "student": "STU-000001",
        "from_status": "ACTIVE",
        "to_status": "ON_HOLD",
        "event_date": "2026-09-20",
        "reason": "FINANCIAL_HOLD",
        "actor": body["event"]["actor"],
        "source_ref": None,
    }
    r = admin_api.post(url, {"to_status": "GRADUATED", "reason": "X"}, format="json")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "ILLEGAL_TRANSITION"
    assert err["details"]["allowed"] == ["ACTIVE"]


def test_status_change_validation(admin_api, student):
    url = f"{URL}{student.student_id}/status/"
    assert (
        admin_api.post(url, {"to_status": "SUSPENDED", "reason": "X"}, format="json").status_code
        == 400
    )
    assert admin_api.post(url, {"to_status": "ON_HOLD"}, format="json").status_code == 400
    r = admin_api.post(
        url, {"to_status": "ON_HOLD", "reason": "X", "event_date": "2025-12-31"}, format="json"
    )
    assert r.status_code == 400 and "event_date" in r.json()["error"]["details"]


def test_filters_pagination_and_ordering(admin_api, make_student, make_program, terms):
    other = make_program(code="MBA-1", level="PG")
    make_student(residency="INTERNATIONAL")
    make_student(program=other)
    s3 = make_student()
    admin_api.post(
        f"{URL}{s3.student_id}/status/", {"to_status": "ON_HOLD", "reason": "X"}, format="json"
    )
    ids = lambda r: [s["student_id"] for s in r.json()["results"]]  # noqa: E731
    assert ids(admin_api.get(URL, {"residency": "INTERNATIONAL"})) == ["STU-000001"]
    assert ids(admin_api.get(URL, {"program": other.program_id})) == ["STU-000002"]
    assert ids(admin_api.get(URL, {"status": "ON_HOLD"})) == ["STU-000003"]
    page = admin_api.get(URL, {"page_size": 2}).json()
    assert page["count"] == 3 and len(page["results"]) == 2 and page["next"]
    assert ids(admin_api.get(URL, {"ordering": "-student_id"}))[0] == "STU-000003"
    bad = admin_api.get(URL, {"status": "SUSPENDED"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_authentication_and_roles(api_client_for, student, setup):
    from rest_framework.test import APIClient

    anon = APIClient().get(URL)
    assert anon.status_code == 401 and anon.json()["error"]["code"] == "NOT_AUTHENTICATED"
    staff = api_client_for("support_staff")
    assert staff.get(URL).status_code == 200
    r = staff.post(URL, _payload(), format="json")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"
    for role in ("learner", "instructor"):
        assert api_client_for(role).get(URL).status_code == 403
    assert (
        api_client_for("platform_admin")
        .post(URL, _payload(email="p@example.edu"), format="json")
        .status_code
        == 201
    )
    assert api_client_for(superuser=True).get(URL).status_code == 200
