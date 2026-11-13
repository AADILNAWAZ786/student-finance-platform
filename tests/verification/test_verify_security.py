"""V-SEC: authentication, the role matrix, CSRF, secrets, and the learner/instructor gap.

Role matrix implemented in Phase 1 (core.permissions), W = platform_admin + academic_admin:
  students, enrollments  read: platform_admin, academic_admin, support_staff   write: W
  programs, terms        read: the same three + instructor                     write: W
  health                 public
Phase 2 billing (decision D6): fee schedules, invoices, billing batches and the student
ledger/statement/balance: read platform_admin + support_staff, write platform_admin only.
Academic administrators and instructors get no financial data; learners stay blocked (L1).
Superusers pass everything. Users without a role are denied everything.
"""

import pytest
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

ROLES = ["platform_admin", "academic_admin", "support_staff", "instructor", "learner"]
STAFF = {"platform_admin", "academic_admin", "support_staff"}
WRITERS = {"platform_admin", "academic_admin"}
BILLING_READERS = {"platform_admin", "support_staff"}
BILLING_WRITERS = {"platform_admin"}
FEE = "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
INV = "INV-STU-000001-2026-000001"

# (operation id, method, path, body factory, roles allowed)
OPS = [
    ("students.list", "get", "/api/v1/students/", None, STAFF),
    ("students.retrieve", "get", "/api/v1/students/STU-000001/", None, STAFF),
    ("students.create", "post", "/api/v1/students/", "student", WRITERS),
    ("students.patch", "patch", "/api/v1/students/STU-000001/", "patch", WRITERS),
    ("students.status", "post", "/api/v1/students/STU-000001/status/", "status", WRITERS),
    ("programs.list", "get", "/api/v1/programs/", None, STAFF | {"instructor"}),
    ("programs.retrieve", "get", "/api/v1/programs/PRG-001/", None, STAFF | {"instructor"}),
    ("programs.create", "post", "/api/v1/programs/", "program", WRITERS),
    ("terms.list", "get", "/api/v1/terms/", None, STAFF | {"instructor"}),
    ("terms.retrieve", "get", "/api/v1/terms/TERM-2026-FALL/", None, STAFF | {"instructor"}),
    ("terms.create", "post", "/api/v1/terms/", "term", WRITERS),
    ("enrollments.list", "get", "/api/v1/enrollments/", None, STAFF),
    ("enrollments.retrieve", "get", "/api/v1/enrollments/ENR-2026-000001/", None, STAFF),
    ("enrollments.events", "get", "/api/v1/enrollments/ENR-2026-000001/events/", None, STAFF),
    ("enrollments.create", "post", "/api/v1/enrollments/", "enrollment", WRITERS),
    # Phase 2: billing
    ("fee_schedules.list", "get", "/api/v1/fee-schedules/", None, BILLING_READERS),
    ("fee_schedules.create", "post", "/api/v1/fee-schedules/", "fee_schedule", BILLING_WRITERS),
    (
        "fee_schedules.new_version",
        "post",
        f"/api/v1/fee-schedules/{FEE}/new-version/",
        "new_version",
        BILLING_WRITERS,
    ),
    (
        "fee_schedules.history",
        "get",
        f"/api/v1/fee-schedules/{FEE}/history/",
        None,
        BILLING_READERS,
    ),
    ("invoices.list", "get", "/api/v1/invoices/", None, BILLING_READERS),
    ("invoices.retrieve", "get", f"/api/v1/invoices/{INV}/", None, BILLING_READERS),
    ("invoices.generate", "post", "/api/v1/invoices/generate/", "generate", BILLING_WRITERS),
    ("invoices.void", "post", f"/api/v1/invoices/{INV}/void/", "void", BILLING_WRITERS),
    ("late_fees.assess", "post", "/api/v1/billing/late-fees/assess/", "as_of", BILLING_WRITERS),
    (
        "collection.refresh",
        "post",
        "/api/v1/billing/collection-status/refresh/",
        "as_of",
        BILLING_WRITERS,
    ),
    ("students.ledger", "get", "/api/v1/students/STU-000001/ledger/", None, BILLING_READERS),
    ("students.statement", "get", "/api/v1/students/STU-000001/statement/", None, BILLING_READERS),
    ("students.balance", "get", "/api/v1/students/STU-000001/balance/", None, BILLING_READERS),
]
SUCCESS = {"get": 200, "post": 201, "patch": 200}
# documented success code per operation (status change updates a record: 200, not 201)
SUCCESS_BY_OP = {
    "students.status": 200,
    "invoices.void": 200,
    "late_fees.assess": 200,
    "collection.refresh": 200,
}


def _body(kind, n):
    return {
        None: None,
        "student": {
            "first_name": "N",
            "last_name": "M",
            "email": f"new{n}@example.edu",
            "date_of_birth": "2006-01-01",
            "residency": "DOMESTIC",
            "program": "PRG-001",
            "admit_term": "TERM-2026-SPRING",
            "gpa": "3.00",
            "need_index": "0.20",
        },
        "patch": {"first_name": "Changed"},
        "status": {"to_status": "ON_HOLD", "reason": "X", "event_date": "2026-02-01"},
        "program": {
            "code": f"P{n}",
            "name": "P",
            "level": "UG",
            "department": "D",
            "is_stem": False,
            "has_lab": False,
            "min_credits": 6,
            "max_credits": 12,
        },
        "term": {
            "year": 2027,
            "season": "SPRING",
            "is_billable": False,
            "invoice_date": "2027-01-04",
            "due_date": "2027-02-01",
            "start_date": "2027-01-25",
            "census_date": "2027-02-22",
            "end_date": "2027-05-08",
        },
        "enrollment": {
            "student": "STU-000001",
            "term": "TERM-2026-SUMMER",
            "credits": 16,
            "enrolled_on": "2026-05-20",
        },
        "fee_schedule": {
            "program": "PRG-001",
            "term": "TERM-2026-FALL",
            "fee_type": "TECHNOLOGY",
            "residency": "ALL",
            "charge_basis": "FLAT",
            "amount": "4000.00",
            "effective_from": "2026-01-01",
        },
        "new_version": {"amount": "9360.00", "effective_from": "2026-08-01"},
        "generate": {"enrollment_id": "ENR-2026-000002"},
        "void": {"void_date": "2026-09-08"},  # the world invoice is issued 2026-09-07
        "as_of": {"as_of_date": "2026-09-29"},
    }[kind]


@pytest.fixture
def world(student, terms, program, make_student):
    from academics import services
    from billing import services as billing
    from tests.billing_fixtures import FALL_PRICES, make_fees

    enrollment = services.enroll(
        student=student,
        term=terms["TERM-2026-FALL"],
        credits=16,
        enrolled_on=terms["TERM-2026-FALL"].start_date,
        actor="t",
    )
    # Phase 2: FALL fees (no TECHNOLOGY, so creating it is a valid V1), STU-000001's
    # invoice, and a second student's FALL enrollment with no invoice yet
    make_fees(program, terms["TERM-2026-FALL"], [p for p in FALL_PRICES if p[0] != "TECHNOLOGY"])
    billing.generate_invoice(enrollment=enrollment, actor="t")
    other = services.enroll(
        student=make_student(),
        term=terms["TERM-2026-FALL"],
        credits=16,
        enrolled_on=terms["TERM-2026-FALL"].start_date,
        actor="t",
    )
    assert other.enrollment_id == "ENR-2026-000002"


def _call(client, method, path, body):
    return (
        getattr(client, method)(path, body, format="json")
        if body
        else getattr(client, method)(path)
    )


MATRIX = [
    (op, method, path, kind, allowed, role)
    for op, method, path, kind, allowed in OPS
    for role in [*ROLES, "no_role", "superuser"]
]


@pytest.mark.parametrize(
    ("op", "method", "path", "kind", "allowed", "role"),
    MATRIX,
    ids=[f"{m[0]}-{m[5]}" for m in MATRIX],
)
def test_role_matrix(world, api_client_for, op, method, path, kind, allowed, role):
    if role == "superuser":
        client, expect_ok = api_client_for(superuser=True), True
    elif role == "no_role":
        client, expect_ok = api_client_for(None), False
    else:
        client, expect_ok = api_client_for(role), role in allowed
    r = _call(client, method, path, _body(kind, len(role)))
    if expect_ok:
        assert r.status_code == SUCCESS_BY_OP.get(op, SUCCESS[method]), r.content
    else:
        assert r.status_code == 403
        assert r.json()["error"] == {
            "code": "PERMISSION_DENIED",
            "rule_id": None,
            "message": "Your role does not allow this action.",
            "details": {},
        }


@pytest.mark.parametrize(("op", "method", "path", "kind", "allowed"), OPS, ids=[o[0] for o in OPS])
def test_unauthenticated_401(world, op, method, path, kind, allowed):
    r = _call(APIClient(), method, path, _body(kind, 1))
    assert r.status_code == 401 and r["WWW-Authenticate"] == "Token"
    assert r.json()["error"]["code"] == "NOT_AUTHENTICATED"


@pytest.mark.parametrize(
    ("header", "code"),
    [
        ("Token 0000000000000000000000000000000000000000", "AUTHENTICATION_FAILED"),
        ("Token", "AUTHENTICATION_FAILED"),  # no key
        ("Token a b", "AUTHENTICATION_FAILED"),  # spaces in key
        ("token 0000000000000000000000000000000000000000", "AUTHENTICATION_FAILED"),
        ("Bearer 0000000000000000000000000000000000000000", "NOT_AUTHENTICATED"),  # other scheme
        ("Basic dXNlcjpwYXNz", "NOT_AUTHENTICATED"),
    ],
)
def test_invalid_credentials_401(world, header, code):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=header)
    r = c.get("/api/v1/students/")
    assert r.status_code == 401 and r.json()["error"]["code"] == code


def test_valid_token_authenticates(world, api_client_for):
    assert api_client_for("support_staff").get("/api/v1/students/").status_code == 200


def test_inactive_user_token_rejected(world):
    u = User.objects.create_user("gone", is_active=False)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=u).key}")
    r = c.get("/api/v1/students/")
    assert r.status_code == 401 and r.json()["error"]["code"] == "AUTHENTICATION_FAILED"


def test_deleted_token_stops_working(world, api_client_for):
    c = api_client_for("academic_admin")
    assert c.get("/api/v1/students/").status_code == 200
    Token.objects.all().delete()
    assert c.get("/api/v1/students/").status_code == 401


def test_session_auth_requires_csrf_for_writes(world):
    from django.contrib.auth.models import Group

    u = User.objects.create_user("sess")
    u.groups.add(Group.objects.get_or_create(name="academic_admin")[0])
    c = APIClient(enforce_csrf_checks=True)
    c.force_login(u)
    assert c.get("/api/v1/students/").status_code == 200
    r = c.post("/api/v1/programs/", _body("program", 9), format="json")
    assert r.status_code == 403 and "CSRF" in r.json()["error"]["message"]


def test_tokens_never_appear_in_responses(world, api_client_for):
    c = api_client_for("platform_admin")
    keys = list(Token.objects.values_list("key", flat=True))
    bodies = [c.get(p).content.decode() for _, m, p, _, _ in OPS if m == "get"]
    bodies.append(c.get("/api/schema/?format=json").content.decode())
    assert keys and not any(k in b for k in keys for b in bodies)


def test_actor_comes_from_authenticated_user_not_payload(world, api_client_for):
    c = api_client_for("academic_admin")
    body = {**_body("status", 1), "actor": "attacker"}
    r = c.post("/api/v1/students/STU-000001/status/", body, format="json")
    assert r.status_code == 400 and "actor" in r.json()["error"]["details"]
    r = c.post("/api/v1/students/STU-000001/status/", _body("status", 1), format="json")
    assert r.json()["event"]["actor"].startswith("academic_admin_")


# --- known limitation (ARCHITECTURE §13): reported, not claimed --------------------------


def test_current_behaviour_learner_and_instructor_have_no_record_access(world, api_client_for):
    learner, instructor = api_client_for("learner"), api_client_for("instructor")
    assert learner.get("/api/v1/students/STU-000001/").status_code == 403
    assert learner.get("/api/v1/programs/").status_code == 403
    assert instructor.get("/api/v1/enrollments/").status_code == 403
    assert instructor.get("/api/v1/programs/").status_code == 200


def test_D6_academic_admin_and_instructor_get_no_financial_data(world, api_client_for):
    for role in ("academic_admin", "instructor", "learner"):
        client = api_client_for(role)
        for _, method, path, _, allowed in OPS:
            if allowed in (BILLING_READERS, BILLING_WRITERS) and method == "get":
                assert client.get(path).status_code == 403, (role, path)


@pytest.mark.xfail(
    strict=True,
    reason="BLOCKED (design gap): the frozen data model has no "
    "User↔Student link, so 'learner reads own student record' (ARCHITECTURE "
    "§13) cannot be implemented without a model change.",
)
def test_design_learner_reads_own_record(world, api_client_for):
    assert api_client_for("learner").get("/api/v1/students/STU-000001/").status_code == 200


@pytest.mark.xfail(
    strict=True,
    reason="BLOCKED (design gap): the frozen data model has no "
    "User↔Program link, so 'instructor reads enrollments for their programs' "
    "(ARCHITECTURE §13) cannot be implemented without a model change.",
)
def test_design_instructor_reads_enrollments_of_own_program(world, api_client_for):
    assert api_client_for("instructor").get("/api/v1/enrollments/").status_code == 200
