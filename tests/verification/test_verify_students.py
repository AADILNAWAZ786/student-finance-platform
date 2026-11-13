"""V-STU: /students/ endpoints. Every response is also validated against the OpenAPI schema."""

from datetime import date

import pytest

from students.models import Student, StudentStatusEvent

pytestmark = pytest.mark.django_db

L, D = "/api/v1/students/", "/api/v1/students/{student_id}/"
S = "/api/v1/students/{student_id}/status/"
REQUIRED = [
    "first_name",
    "last_name",
    "email",
    "date_of_birth",
    "residency",
    "program",
    "admit_term",
    "gpa",
    "need_index",
]


def body(**kw):
    b = {
        "first_name": "Asha",
        "last_name": "Rao",
        "email": "asha@example.edu",
        "date_of_birth": "2006-03-14",
        "residency": "DOMESTIC",
        "program": "PRG-001",
        "admit_term": "TERM-2026-SPRING",
        "gpa": "3.72",
        "need_index": "0.35",
    }
    b.update(kw)
    return {k: v for k, v in b.items() if v is not ...}


@pytest.fixture
def api(admin_api, terms, program, frozen_today):
    frozen_today(date(2026, 1, 5))
    return admin_api


def err(r, status, code="VALIDATION_ERROR"):
    assert r.status_code == status, r.content
    e = r.json()["error"]
    assert e["code"] == code and set(e) == {"code", "rule_id", "message", "details"}
    return e["details"]


# --- POST /students/: valid --------------------------------------------------------


def test_create_valid(api, assert_matches_openapi):
    r = api.post(L, body(), format="json")
    assert_matches_openapi(r, L, "post")
    assert r.status_code == 201
    assert r.json() == {**body(), "student_id": "STU-000001", "status": "ACTIVE"}
    ev = StudentStatusEvent.objects.get()
    assert (ev.student_id, ev.from_status, ev.to_status, ev.reason, ev.event_date) == (
        "STU-000001",
        None,
        "ACTIVE",
        "CREATED",
        date(2026, 1, 5),
    )


@pytest.mark.parametrize("field", REQUIRED)
def test_create_missing_required_field(api, field):
    d = err(api.post(L, body(**{field: ...}), format="json"), 400)
    assert d[field] == ["This field is required."]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gpa", 3.72),
        ("gpa", 4),
        ("need_index", 0.35),
        ("gpa", "abc"),
        ("gpa", "NaN"),
        ("gpa", "Infinity"),
        ("gpa", None),
        ("date_of_birth", "14/03/2006"),
        ("date_of_birth", "2006-02-30"),
        ("date_of_birth", 20060314),
        ("email", "asha"),
        ("email", "asha@"),
        ("first_name", ""),
        ("first_name", "   "),
        ("first_name", None),
        ("first_name", "x" * 61),
        ("last_name", "y" * 61),
        ("email", "a" * 110 + "@example.edu"),
        ("program", None),
        ("admit_term", ["TERM-2026-SPRING"]),
        ("residency", None),
    ],
)
def test_create_invalid_types_and_formats(api, field, value):
    assert field in err(api.post(L, body(**{field: value}), format="json"), 400)


@pytest.mark.parametrize("value", ["domestic", "RESIDENT", "", 1])
def test_create_invalid_residency_enum(api, value):
    assert "residency" in err(api.post(L, body(residency=value), format="json"), 400)


@pytest.mark.parametrize("field", ["student_id", "status", "recorded_at", "actor", "foo"])
def test_create_rejects_unknown_and_server_controlled_fields(api, field):
    d = err(api.post(L, body(**{field: "X"}), format="json"), 400)
    assert d == {field: ["Unknown field."]}
    assert Student.objects.count() == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("program", "PRG-999"),
        ("program", "prg-001"),
        ("program", "nonsense"),
        ("admit_term", "TERM-2030-FALL"),
        ("admit_term", "TERM-2026-WINTER"),
    ],
)
def test_create_invalid_foreign_keys(api, field, value):
    d = err(api.post(L, body(**{field: value}), format="json"), 400)
    assert "does not exist" in d[field][0]


@pytest.mark.parametrize(
    "raw,ctype,status,code",
    [
        (b"{not json", "application/json", 400, "PARSE_ERROR"),
        (b"[1, 2]", "application/json", 400, "VALIDATION_ERROR"),
        (b"null", "application/json", 400, "VALIDATION_ERROR"),
        (b"first_name=A", "application/x-www-form-urlencoded", 415, "UNSUPPORTED_MEDIA_TYPE"),
    ],
)
def test_create_malformed_bodies(api, raw, ctype, status, code):
    r = api.generic("POST", L, raw, content_type=ctype)
    err(r, status, code)


# --- business rules: decimals, age, email, program -----------------------------------


@pytest.mark.parametrize(
    ("gpa", "need", "ok", "out"),
    [
        ("0.00", "0.00", True, ("0.00", "0.00")),
        ("4.00", "1.00", True, ("4.00", "1.00")),
        ("4", "1", True, ("4.00", "1.00")),
        ("3.7", "0.5", True, ("3.70", "0.50")),
        ("-0.00", "-0.00", True, ("0.00", "0.00")),
        ("4.01", "0.5", False, None),
        ("-0.01", "0.5", False, None),
        ("3.00", "1.01", False, None),
        ("3.00", "-0.01", False, None),
        ("3.725", "0.5", False, None),
        ("3.00", "0.555", False, None),
    ],
)
def test_gpa_and_need_index_ranges(api, gpa, need, ok, out):
    r = api.post(L, body(gpa=gpa, need_index=need), format="json")
    if ok:
        assert r.status_code == 201, r.content
        assert (r.json()["gpa"], r.json()["need_index"]) == out
    else:
        err(r, 400)


# SPRING 2026 starts Mon 26 Jan 2026; historical TERM-2024-FALL starts Mon 09 Sep 2024
@pytest.mark.parametrize(
    ("dob", "admit", "ok"),
    [
        ("2010-01-26", "TERM-2026-SPRING", True),  # 16th birthday on start day
        ("2010-01-27", "TERM-2026-SPRING", False),  # 15 years 364 days
        ("1965-01-27", "TERM-2026-SPRING", True),  # 60 (turns 61 the next day)
        ("1965-01-26", "TERM-2026-SPRING", False),  # 61st birthday on start day
        ("2008-09-09", "TERM-2024-FALL", True),  # 16 on historical admit term start
        ("2008-09-10", "TERM-2024-FALL", False),
        ("2030-01-01", "TERM-2026-SPRING", False),  # future date of birth
    ],
)
def test_age_16_to_60_on_admit_term_start(api, dob, admit, ok):
    r = api.post(L, body(date_of_birth=dob, admit_term=admit), format="json")
    if ok:
        assert r.status_code == 201, r.content
    else:
        assert "date_of_birth" in err(r, 400)


@pytest.mark.parametrize(
    ("email", "ok", "stored"),
    [
        ("asha@example.edu", True, "asha@example.edu"),
        ("Asha.Rao@EXAMPLE.EDU", True, "Asha.Rao@example.edu"),
        ("asha@gmail.com", False, None),
        ("asha@example.com", False, None),
        ("asha@mail.example.edu", False, None),
        ("asha@example.edu.evil.com", False, None),
    ],
)
def test_reserved_email_domain(api, email, ok, stored):
    r = api.post(L, body(email=email), format="json")
    if ok:
        assert r.status_code == 201 and r.json()["email"] == stored
    else:
        assert "example.edu" in err(r, 400)["email"][0]


@pytest.mark.parametrize(
    "second", ["asha@example.edu", "ASHA@example.edu", "asha@EXAMPLE.edu", "Asha@Example.Edu"]
)
def test_email_uniqueness_is_case_insensitive(api, second):
    assert api.post(L, body(), format="json").status_code == 201
    d = err(api.post(L, body(email=second), format="json"), 400)
    assert "already exists" in d["email"][0]
    assert Student.objects.count() == 1


def test_inactive_program_rejected(api, make_program):
    p = make_program(code="OLD", is_active=False)
    assert "program" in err(api.post(L, body(program=p.program_id), format="json"), 400)


def test_historical_admit_term_accepted(api):
    r = api.post(L, body(admit_term="TERM-2023-SPRING", date_of_birth="2004-05-01"), format="json")
    assert r.status_code == 201 and r.json()["admit_term"] == "TERM-2023-SPRING"


def test_ids_are_sequential_and_server_assigned(api):
    ids = [
        api.post(L, body(email=f"s{i}@example.edu"), format="json").json()["student_id"]
        for i in range(3)
    ]
    assert ids == ["STU-000001", "STU-000002", "STU-000003"]


# --- GET list: pagination, filters, ordering ---------------------------------------


@pytest.fixture
def five(api, make_student, make_program, terms):
    mba = make_program(code="MBA", level="PG")
    make_student(residency="INTERNATIONAL")  # 1
    make_student(program=mba)  # 2
    make_student(admit_term=terms["TERM-2025-FALL"])  # 3
    make_student(residency="INTERNATIONAL", program=mba)  # 4
    s5 = make_student()  # 5
    api.post(
        D.format(student_id=s5.student_id) + "status/",
        {"to_status": "ON_HOLD", "reason": "X"},
        format="json",
    )
    return api


def ids(r):
    return [s["student_id"] for s in r.json()["results"]]


def test_list_valid_and_schema(five, assert_matches_openapi):
    r = five.get(L)
    assert_matches_openapi(r, L, "get")
    assert r.json()["count"] == 5 and r.json()["previous"] is None


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"residency": "INTERNATIONAL"}, ["STU-000001", "STU-000004"]),
        ({"program": "PRG-002"}, ["STU-000002", "STU-000004"]),
        ({"program": "PRG-002", "residency": "INTERNATIONAL"}, ["STU-000004"]),
        ({"status": "ON_HOLD"}, ["STU-000005"]),
        ({"status": "ACTIVE", "residency": "DOMESTIC"}, ["STU-000002", "STU-000003"]),
        ({"admit_term": "TERM-2025-FALL"}, ["STU-000003"]),
        ({"program": "PRG-999"}, []),
        (
            {"ordering": "-student_id"},
            ["STU-000005", "STU-000004", "STU-000003", "STU-000002", "STU-000001"],
        ),
        (
            {"ordering": "not_a_field"},
            ["STU-000001", "STU-000002", "STU-000003", "STU-000004", "STU-000005"],
        ),
        (
            {"unknown_param": "x"},
            ["STU-000001", "STU-000002", "STU-000003", "STU-000004", "STU-000005"],
        ),
    ],
)
def test_list_filters_and_ordering(five, params, expected):
    assert ids(five.get(L, params)) == expected


@pytest.mark.parametrize(
    ("params", "field"),
    [({"status": "SUSPENDED"}, "status"), ({"residency": "domestic"}, "residency")],
)
def test_list_invalid_filter_value_400(five, params, field, assert_matches_openapi):
    r = five.get(L, params)
    assert_matches_openapi(r, L, "get")
    assert field in err(r, 400)


def test_pagination(five, assert_matches_openapi):
    p1 = five.get(L, {"page_size": 2}).json()
    assert p1["count"] == 5 and len(p1["results"]) == 2 and "page=2" in p1["next"]
    p3 = five.get(L, {"page_size": 2, "page": 3}).json()
    assert ids_from(p3) == ["STU-000005"] and p3["next"] is None
    assert len(five.get(L, {"page_size": 1000}).json()["results"]) == 5  # capped at 500
    assert len(five.get(L, {"page_size": "abc"}).json()["results"]) == 5  # default 50
    r = five.get(L, {"page": 99})
    assert_matches_openapi(r, L, "get")
    err(r, 404, "NOT_FOUND")


def ids_from(page):
    return [s["student_id"] for s in page["results"]]


def test_page_size_maximum_is_500(api, make_student):
    from core.api import StandardPagination

    assert StandardPagination.max_page_size == 500 and StandardPagination.page_size == 50


def test_missing_trailing_slash_redirects(five):
    r = five.get("/api/v1/students")
    assert r.status_code == 301 and r["Location"].endswith("/api/v1/students/")


# --- GET detail ----------------------------------------------------------------------


def test_detail_valid_and_404(api, student, assert_matches_openapi):
    r = api.get(D.format(student_id=student.student_id))
    assert_matches_openapi(r, D, "get")
    assert r.json()["student_id"] == student.student_id
    for missing in ("STU-999999", "stu-000001", "STU-1"):
        r = api.get(D.format(student_id=missing))
        assert_matches_openapi(r, D, "get")
        err(r, 404, "NOT_FOUND")


# --- PATCH ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"first_name": "Asha M"}, {"first_name": "Asha M"}),
        ({"last_name": " Iyer "}, {"last_name": "Iyer"}),
        ({"email": "New.Addr@EXAMPLE.edu"}, {"email": "New.Addr@example.edu"}),
        (
            {"first_name": "A", "last_name": "B", "email": "ab@example.edu"},
            {"first_name": "A", "last_name": "B", "email": "ab@example.edu"},
        ),
    ],
)
def test_patch_contact_fields(api, student, change, expected, assert_matches_openapi):
    r = api.patch(D.format(student_id=student.student_id), change, format="json")
    assert_matches_openapi(r, D, "patch")
    assert r.status_code == 200
    assert {k: r.json()[k] for k in expected} == expected
    assert r.json()["gpa"] == "3.72" and r.json()["status"] == "ACTIVE"  # untouched


def test_patch_own_email_unchanged_is_allowed(api, student):
    r = api.patch(D.format(student_id=student.student_id), {"email": student.email}, format="json")
    assert r.status_code == 200


@pytest.mark.parametrize(
    "field",
    [
        "gpa",
        "need_index",
        "status",
        "program",
        "admit_term",
        "residency",
        "date_of_birth",
        "student_id",
        "recorded_at",
    ],
)
def test_patch_protected_fields_rejected(api, student, field):
    d = err(api.patch(D.format(student_id=student.student_id), {field: "X"}, format="json"), 400)
    assert "Only contact fields" in d[field][0]
    student.refresh_from_db()
    assert (student.status, str(student.gpa)) == ("ACTIVE", "3.72")


@pytest.mark.parametrize(
    "change",
    [{}, {"first_name": ""}, {"email": "bad"}, {"email": "x@gmail.com"}, {"last_name": "z" * 61}],
)
def test_patch_invalid(api, student, change):
    err(api.patch(D.format(student_id=student.student_id), change, format="json"), 400)


def test_patch_duplicate_email_case_insensitive(api, make_student):
    a, b = make_student(), make_student()
    d = err(
        api.patch(D.format(student_id=b.student_id), {"email": a.email.upper()}, format="json"), 400
    )
    assert "already exists" in d["email"][0]


def test_patch_nonexistent_404(api):
    err(
        api.patch(D.format(student_id="STU-999999"), {"first_name": "A"}, format="json"),
        404,
        "NOT_FOUND",
    )


# --- POST status ---------------------------------------------------------------------

ALLOWED_EDGES = [
    ("ACTIVE", "ON_HOLD"),
    ("ON_HOLD", "ACTIVE"),
    ("ACTIVE", "WITHDRAWN"),
    ("ACTIVE", "GRADUATED"),
]
STATES = ["ACTIVE", "ON_HOLD", "WITHDRAWN", "GRADUATED"]
PATH_TO = {
    "ACTIVE": [],
    "ON_HOLD": ["ON_HOLD"],
    "WITHDRAWN": ["WITHDRAWN"],
    "GRADUATED": ["GRADUATED"],
}
ILLEGAL_EDGES = [(a, b) for a in STATES for b in STATES if (a, b) not in ALLOWED_EDGES]


def _move(api, sid, to, day=10, reason="T"):
    return api.post(
        S.format(student_id=sid),
        {"to_status": to, "reason": reason, "event_date": f"2026-02-{day:02d}"},
        format="json",
    )


@pytest.mark.parametrize(("frm", "to"), ALLOWED_EDGES)
def test_every_allowed_transition_writes_one_event(api, student, frm, to, assert_matches_openapi):
    for step in PATH_TO[frm]:
        assert _move(api, student.student_id, step, day=1).status_code == 200
    before = student.status_events.count()
    r = _move(api, student.student_id, to, reason="REASON_X")
    assert_matches_openapi(r, S, "post")
    assert r.status_code == 200
    ev = r.json()["event"]
    assert (ev["from_status"], ev["to_status"], ev["reason"], ev["event_date"]) == (
        frm,
        to,
        "REASON_X",
        "2026-02-10",
    )
    assert r.json()["student"]["status"] == to
    assert student.status_events.count() == before + 1


@pytest.mark.parametrize(("frm", "to"), ILLEGAL_EDGES)
def test_every_illegal_transition_rejected_409(api, student, frm, to, assert_matches_openapi):
    for step in PATH_TO[frm]:
        assert _move(api, student.student_id, step, day=1).status_code == 200
    before = student.status_events.count()
    r = _move(api, student.student_id, to)
    assert_matches_openapi(r, S, "post")
    d = err(r, 409, "ILLEGAL_TRANSITION")
    assert (d["from_status"], d["to_status"]) == (frm, to)
    student.refresh_from_db()
    assert student.status == frm and student.status_events.count() == before


def test_full_history_is_append_only_and_ordered(api, student):
    for i, to in enumerate(["ON_HOLD", "ACTIVE", "ON_HOLD", "ACTIVE", "GRADUATED"], start=1):
        assert _move(api, student.student_id, to, day=i).status_code == 200
    chain = list(student.status_events.order_by("event_id").values_list("from_status", "to_status"))
    assert chain == [
        (None, "ACTIVE"),
        ("ACTIVE", "ON_HOLD"),
        ("ON_HOLD", "ACTIVE"),
        ("ACTIVE", "ON_HOLD"),
        ("ON_HOLD", "ACTIVE"),
        ("ACTIVE", "GRADUATED"),
    ]


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"to_status": "SUSPENDED", "reason": "X"}, "to_status"),
        ({"to_status": "on_hold", "reason": "X"}, "to_status"),
        ({"reason": "X"}, "to_status"),
        ({"to_status": "ON_HOLD"}, "reason"),
        ({"to_status": "ON_HOLD", "reason": "x" * 41}, "reason"),
        ({"to_status": "ON_HOLD", "reason": "X", "event_date": "2026-13-01"}, "event_date"),
        ({"to_status": "ON_HOLD", "reason": "X", "event_date": "2026-01-04"}, "event_date"),
        ({"to_status": "ON_HOLD", "reason": "X", "actor": "someone"}, "actor"),
    ],
)
def test_status_validation_400(api, student, payload, field):
    assert field in err(
        api.post(S.format(student_id=student.student_id), payload, format="json"), 400
    )
    assert student.status_events.count() == 1


def test_status_same_day_as_previous_event_allowed_and_default_date(api, student):
    r = api.post(
        S.format(student_id=student.student_id),
        {"to_status": "ON_HOLD", "reason": "X", "event_date": "2026-01-05"},
        format="json",
    )
    assert r.status_code == 200
    r = api.post(
        S.format(student_id=student.student_id),
        {"to_status": "ACTIVE", "reason": "X"},
        format="json",
    )
    assert r.json()["event"]["event_date"] == "2026-01-05"  # frozen 'today'


def test_status_actor_is_the_authenticated_user(api, student):
    r = api.post(
        S.format(student_id=student.student_id),
        {"to_status": "ON_HOLD", "reason": "X"},
        format="json",
    )
    assert r.json()["event"]["actor"].startswith("academic_admin_")


def test_status_nonexistent_student_404(api):
    err(
        api.post(
            S.format(student_id="STU-999999"),
            {"to_status": "ON_HOLD", "reason": "X"},
            format="json",
        ),
        404,
        "NOT_FOUND",
    )
