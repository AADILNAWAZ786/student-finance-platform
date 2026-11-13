"""V-PRG / V-TRM: /programs/ and /terms/ endpoints, validated against the OpenAPI schema."""

from datetime import date, timedelta

import pytest

from academics.models import Program, Term

pytestmark = pytest.mark.django_db

PL, PD = "/api/v1/programs/", "/api/v1/programs/{program_id}/"
TL, TD = "/api/v1/terms/", "/api/v1/terms/{term_id}/"
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
P_REQUIRED = [
    "code",
    "name",
    "level",
    "department",
    "is_stem",
    "has_lab",
    "min_credits",
    "max_credits",
]


def err(r, status, code="VALIDATION_ERROR"):
    assert r.status_code == status, r.content
    assert r.json()["error"]["code"] == code
    return r.json()["error"]["details"]


def prog(**kw):
    b = {**PROGRAM, **kw}
    return {k: v for k, v in b.items() if v is not ...}


# --- programs ----------------------------------------------------------------------


def test_program_create_valid(admin_api, assert_matches_openapi):
    r = admin_api.post(PL, PROGRAM, format="json")
    assert_matches_openapi(r, PL, "post")
    assert r.status_code == 201 and r.json() == {"program_id": "PRG-001", **PROGRAM}


def test_program_is_active_defaults_true(admin_api):
    r = admin_api.post(PL, prog(is_active=...), format="json")
    assert r.status_code == 201 and r.json()["is_active"] is True


@pytest.mark.parametrize("field", P_REQUIRED)
def test_program_missing_required(admin_api, field):
    assert field in err(admin_api.post(PL, prog(**{field: ...}), format="json"), 400)


@pytest.mark.parametrize(
    ("lo", "hi", "ok"),
    [
        (6, 6, True),
        (6, 24, True),
        (12, 12, True),
        (24, 24, True),
        (5, 20, False),
        (12, 25, False),
        (21, 20, False),
        (0, 6, False),
        (-1, 6, False),
        (6, 32768, False),
        ("abc", 20, False),
        (12.5, 20, False),
        (None, 20, False),
    ],
)
def test_program_credit_boundaries(admin_api, lo, hi, ok):
    r = admin_api.post(PL, prog(min_credits=lo, max_credits=hi), format="json")
    if ok:
        assert r.status_code == 201, r.content
    else:
        err(r, 400)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("level", "PHD"),
        ("level", "ug"),
        ("level", ""),
        ("is_stem", "maybe"),
        ("is_stem", None),
        ("code", ""),
        ("code", "C" * 13),
        ("name", "N" * 121),
        ("department", ""),
        ("department", "D" * 61),
        ("is_active", "sometimes"),
    ],
)
def test_program_invalid_enum_and_formats(admin_api, field, value):
    assert field in err(admin_api.post(PL, prog(**{field: value}), format="json"), 400)


@pytest.mark.parametrize(
    ("field", "value"), [("code", "C" * 12), ("name", "N" * 120), ("department", "D" * 60)]
)
def test_program_max_lengths_accepted(admin_api, field, value):
    assert admin_api.post(PL, prog(**{field: value}), format="json").status_code == 201


@pytest.mark.parametrize("field", ["program_id", "recorded_at", "foo"])
def test_program_server_controlled_or_unknown_fields(admin_api, field):
    assert err(admin_api.post(PL, prog(**{field: "X"}), format="json"), 400) == {
        field: ["Unknown field."]
    }
    assert Program.objects.count() == 0


def test_program_duplicate_code(admin_api):
    assert admin_api.post(PL, PROGRAM, format="json").status_code == 201
    assert "already exists" in err(admin_api.post(PL, PROGRAM, format="json"), 400)["code"][0]
    assert Program.objects.count() == 1


@pytest.fixture
def programs(admin_api):
    for kw in [
        {},
        {
            "code": "MBA",
            "level": "PG",
            "is_stem": False,
            "has_lab": False,
            "department": "Business",
            "name": "MBA",
        },
        {
            "code": "DIP-ART",
            "level": "DIPLOMA",
            "is_stem": False,
            "has_lab": False,
            "department": "Arts",
            "name": "Diploma Arts",
            "is_active": False,
        },
    ]:
        assert admin_api.post(PL, prog(**kw), format="json").status_code == 201
    return admin_api


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, ["PRG-001", "PRG-002", "PRG-003"]),
        ({"level": "PG"}, ["PRG-002"]),
        ({"is_stem": "true"}, ["PRG-001"]),
        ({"has_lab": "false"}, ["PRG-002", "PRG-003"]),
        ({"is_active": "false"}, ["PRG-003"]),
        ({"department": "business"}, ["PRG-002"]),  # case-insensitive
        ({"ordering": "-code"}, ["PRG-002", "PRG-003", "PRG-001"]),
        ({"ordering": "name"}, ["PRG-001", "PRG-003", "PRG-002"]),
        ({"page_size": 1, "page": 2}, ["PRG-002"]),
    ],
)
def test_program_list_filters_ordering_pagination(
    programs, params, expected, assert_matches_openapi
):
    r = programs.get(PL, params)
    assert_matches_openapi(r, PL, "get")
    assert [p["program_id"] for p in r.json()["results"]] == expected


@pytest.mark.parametrize("params", [{"level": "PHD"}, {"page": 9}])
def test_program_list_errors(programs, params, assert_matches_openapi):
    r = programs.get(PL, params)
    assert_matches_openapi(r, PL, "get")
    assert r.status_code in (400, 404)


def test_program_detail_and_404(programs, assert_matches_openapi):
    r = programs.get(PD.format(program_id="PRG-002"))
    assert_matches_openapi(r, PD, "get")
    assert r.json()["code"] == "MBA"
    for missing in ("PRG-999", "prg-002", "PRG-2"):
        r = programs.get(PD.format(program_id=missing))
        assert_matches_openapi(r, PD, "get")
        err(r, 404, "NOT_FOUND")


# --- terms -------------------------------------------------------------------------


def term(year=2027, season="SPRING", start=date(2027, 1, 25), **kw):
    b = {
        "year": year,
        "season": season,
        "is_billable": False,
        "invoice_date": str(start - timedelta(21)),
        "due_date": str(start + timedelta(7)),
        "start_date": str(start),
        "census_date": str(start + timedelta(28)),
        "end_date": str(start + timedelta(103)),
    }
    b.update(kw)
    return {k: v for k, v in b.items() if v is not ...}


T_REQUIRED = [
    "year",
    "season",
    "is_billable",
    "invoice_date",
    "due_date",
    "start_date",
    "census_date",
    "end_date",
]


def test_term_create_valid_derives_id_and_default_grace(admin_api, terms, assert_matches_openapi):
    r = admin_api.post(TL, term(), format="json")
    assert_matches_openapi(r, TL, "post")
    assert r.status_code == 201
    assert r.json() == {**term(), "term_id": "TERM-2027-SPRING", "late_fee_grace_days": 14}


@pytest.mark.parametrize("field", T_REQUIRED)
def test_term_missing_required(admin_api, terms, field):
    assert field in err(admin_api.post(TL, term(**{field: ...}), format="json"), 400)


@pytest.mark.parametrize(("offset", "ok"), [(28, True), (27, False), (29, False)])
def test_census_must_be_start_plus_28(admin_api, terms, offset, ok):
    start = date(2027, 1, 25)
    r = admin_api.post(TL, term(census_date=str(start + timedelta(offset))), format="json")
    if ok:
        assert r.status_code == 201
    else:
        assert "start_date + 28 days (2027-02-22)" in str(err(r, 400)["census_date"])


@pytest.mark.parametrize(
    ("change", "field", "ok"),
    [
        ({"invoice_date": "2027-02-01", "due_date": "2027-02-01"}, None, True),  # equal ok
        ({"invoice_date": "2027-02-02"}, "due_date", False),  # after due
        ({"end_date": "2027-02-22"}, "census_date", False),  # end = census
        ({"end_date": "2027-02-21"}, "census_date", False),  # end < census
        ({"late_fee_grace_days": 0}, None, True),
        ({"late_fee_grace_days": -1}, "late_fee_grace_days", False),
        ({"late_fee_grace_days": "x"}, "late_fee_grace_days", False),
    ],
)
def test_term_date_ordering(admin_api, terms, change, field, ok):
    r = admin_api.post(TL, term(**change), format="json")
    if ok:
        assert r.status_code == 201, r.content
    else:
        assert field in err(r, 400)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("season", "WINTER"),
        ("season", "fall"),
        ("year", 1999),
        ("year", 3000),
        ("year", "x"),
        ("start_date", "2027-02-30"),
        ("start_date", "25-01-2027"),
        ("is_billable", "perhaps"),
    ],
)
def test_term_invalid_enum_and_formats(admin_api, terms, field, value):
    assert field in err(admin_api.post(TL, term(**{field: value}), format="json"), 400)


@pytest.mark.parametrize("field", ["term_id", "recorded_at", "foo"])
def test_term_server_controlled_or_unknown_fields(admin_api, terms, field):
    assert err(admin_api.post(TL, term(**{field: "X"}), format="json"), 400) == {
        field: ["Unknown field."]
    }


def test_term_duplicate(admin_api, terms):
    fall = term(2026, "FALL", date(2026, 9, 7), is_billable=True)
    assert "TERM-2026-FALL already exists" in str(err(admin_api.post(TL, fall, format="json"), 400))
    assert Term.objects.count() == 12


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"is_billable": "true"}, ["TERM-2026-SPRING", "TERM-2026-SUMMER", "TERM-2026-FALL"]),
        (
            {"is_billable": "false", "year": 2023},
            ["TERM-2023-SPRING", "TERM-2023-SUMMER", "TERM-2023-FALL"],
        ),
        ({"season": "FALL", "year": 2025}, ["TERM-2025-FALL"]),
        ({"ordering": "-start_date", "page_size": 2}, ["TERM-2026-FALL", "TERM-2026-SUMMER"]),
        ({"year": 2031}, []),
    ],
)
def test_term_list_filters(admin_api, terms, params, expected, assert_matches_openapi):
    r = admin_api.get(TL, params)
    assert_matches_openapi(r, TL, "get")
    assert [t["term_id"] for t in r.json()["results"]] == expected


def test_term_list_default_order_and_count(admin_api, terms):
    r = admin_api.get(TL).json()
    assert r["count"] == 12 and r["results"][0]["term_id"] == "TERM-2023-SPRING"


def test_term_list_invalid_filter(admin_api, terms, assert_matches_openapi):
    r = admin_api.get(TL, {"season": "WINTER"})
    assert_matches_openapi(r, TL, "get")
    err(r, 400)


def test_term_detail_billable_and_historical(admin_api, terms, assert_matches_openapi):
    for tid, billable in (("TERM-2026-FALL", True), ("TERM-2024-SPRING", False)):
        r = admin_api.get(TD.format(term_id=tid))
        assert_matches_openapi(r, TD, "get")
        assert r.json()["is_billable"] is billable
        assert date.fromisoformat(r.json()["census_date"]) == date.fromisoformat(
            r.json()["start_date"]
        ) + timedelta(28)
    r = admin_api.get(TD.format(term_id="TERM-2030-FALL"))
    assert_matches_openapi(r, TD, "get")
    err(r, 404, "NOT_FOUND")
