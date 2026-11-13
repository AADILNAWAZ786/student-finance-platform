"""Billing API: every endpoint's success path, filters, validation and error envelopes.
Every success response is validated against the served OpenAPI document."""

from datetime import date
from decimal import Decimal

import pytest

from billing import services

pytestmark = pytest.mark.django_db

V1 = "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
INV = "INV-STU-000001-2026-000001"
TUITION_BODY = {
    "program": "PRG-001",
    "term": "TERM-2026-FALL",
    "fee_type": "TUITION",
    "residency": "DOMESTIC",
    "charge_basis": "PER_CREDIT",
    "amount": "9000.00",
    "effective_from": "2026-01-01",
}


def _error(r, status, code):
    assert r.status_code == status, r.content
    body = r.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    return body["error"]


# --- fee schedules --------------------------------------------------------------------------


def test_create_fee_schedule(finance_api, program, terms, assert_matches_openapi):
    r = finance_api.post("/api/v1/fee-schedules/", TUITION_BODY, format="json")
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/fee-schedules/", "post")
    assert r.json() == {
        "fee_schedule_id": V1,
        "program": "PRG-001",
        "term": "TERM-2026-FALL",
        "fee_type": "TUITION",
        "residency": "DOMESTIC",
        "charge_basis": "PER_CREDIT",
        "amount": "9000.00",
        "is_scholarship_eligible": True,
        "is_refundable": True,
        "is_waivable": False,
        "allocation_priority": 10,
        "version": 1,
        "effective_from": "2026-01-01",
        "effective_to": None,
        "supersedes": None,
        "superseded_by": None,
    }


@pytest.mark.parametrize(
    "field",
    [
        "is_waivable",
        "is_scholarship_eligible",
        "is_refundable",
        "allocation_priority",
        "version",
        "supersedes",
    ],
)
def test_D5_clients_cannot_send_server_fields(finance_api, program, terms, field):
    r = finance_api.post("/api/v1/fee-schedules/", {**TUITION_BODY, field: True}, format="json")
    e = _error(r, 400, "VALIDATION_ERROR")
    assert e["details"] == {field: ["Set by the server from fee_type; clients cannot send it."]}


def test_unknown_field_rejected(finance_api, program, terms):
    r = finance_api.post("/api/v1/fee-schedules/", {**TUITION_BODY, "discount": "1"}, format="json")
    assert _error(r, 400, "VALIDATION_ERROR")["details"] == {"discount": ["Unknown field."]}


@pytest.mark.parametrize("amount", [9000, 9000.5, "9000.001", "-1.00", "abc"])
def test_amount_must_be_a_non_negative_2dp_string(finance_api, program, terms, amount):
    r = finance_api.post(
        "/api/v1/fee-schedules/", {**TUITION_BODY, "amount": amount}, format="json"
    )
    assert "amount" in _error(r, 400, "VALIDATION_ERROR")["details"]


def test_residency_mapping_is_a_400(finance_api, program, terms):
    r = finance_api.post(
        "/api/v1/fee-schedules/",
        {**TUITION_BODY, "fee_type": "LAB", "charge_basis": "FLAT"},
        format="json",
    )
    assert _error(r, 400, "VALIDATION_ERROR")["details"] == {
        "residency": ["LAB fee schedules must use residency ALL."]
    }


def test_unknown_program_or_term_is_400(finance_api, program, terms):
    r = finance_api.post(
        "/api/v1/fee-schedules/", {**TUITION_BODY, "program": "PRG-999"}, format="json"
    )
    assert "program" in _error(r, 400, "VALIDATION_ERROR")["details"]


def test_new_version_and_history(finance_api, fee_setup, assert_matches_openapi):
    r = finance_api.post(
        f"/api/v1/fee-schedules/{V1}/new-version/",
        {"amount": "9360.00", "effective_from": "2026-08-01"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/fee-schedules/{fee_schedule_id}/new-version/", "post")
    v2 = r.json()
    assert (v2["fee_schedule_id"], v2["version"], v2["supersedes"]) == (V1[:-1] + "2", 2, V1)
    r = finance_api.get(f"/api/v1/fee-schedules/{v2['fee_schedule_id']}/history/")
    assert r.status_code == 200
    assert_matches_openapi(r, "/api/v1/fee-schedules/{fee_schedule_id}/history/", "get")
    assert [
        (v["version"], v["effective_from"], v["effective_to"], v["superseded_by"]) for v in r.json()
    ] == [
        (1, "2026-01-01", "2026-07-31", v2["fee_schedule_id"]),
        (2, "2026-08-01", None, None),
    ]
    assert r.json()[0]["amount"] == "9000.00"  # V1 unchanged


def test_new_version_rejects_flags_and_unknown_schedule(finance_api, fee_setup):
    r = finance_api.post(
        f"/api/v1/fee-schedules/{V1}/new-version/",
        {"amount": "1.00", "effective_from": "2026-08-01", "is_waivable": True},
        format="json",
    )
    assert "is_waivable" in _error(r, 400, "VALIDATION_ERROR")["details"]
    r = finance_api.post(
        "/api/v1/fee-schedules/FEE-NOPE/new-version/",
        {"amount": "1.00", "effective_from": "2026-08-01"},
        format="json",
    )
    assert _error(r, 404, "NOT_FOUND")["message"] == "No FeeSchedule matches the given query."


def test_D14_no_fee_schedule_detail_route(finance_api, fee_setup):
    r = finance_api.get(f"/api/v1/fee-schedules/{V1}/")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_fee_schedule_filters(finance_api, fee_setup, assert_matches_openapi):
    services.new_fee_schedule_version(
        fee_setup[("TUITION", "DOMESTIC")], amount=Decimal("1"), effective_from=date(2026, 8, 1)
    )
    r = finance_api.get("/api/v1/fee-schedules/", {"fee_type": "TUITION"})
    assert_matches_openapi(r, "/api/v1/fee-schedules/", "get")
    assert r.json()["count"] == 3
    ids = lambda r: [x["fee_schedule_id"] for x in r.json()["results"]]  # noqa: E731
    r = finance_api.get(
        "/api/v1/fee-schedules/",
        {"fee_type": "TUITION", "residency": "DOMESTIC", "in_force_on": "2026-07-31"},
    )
    assert ids(r) == [V1]
    r = finance_api.get(
        "/api/v1/fee-schedules/",
        {"fee_type": "TUITION", "residency": "DOMESTIC", "in_force_on": "2026-08-01"},
    )
    assert ids(r) == [V1[:-1] + "2"]
    r = finance_api.get("/api/v1/fee-schedules/", {"in_force_on": "2025-12-31"})
    assert r.json()["count"] == 0
    r = finance_api.get("/api/v1/fee-schedules/", {"in_force_on": "2026-09-01"})
    assert r.json()["count"] == 8
    r = finance_api.get("/api/v1/fee-schedules/", {"fee_type": "PARKING"})
    assert _error(r, 400, "VALIDATION_ERROR")["details"] == {
        "fee_type": ["Select a valid choice. PARKING is not one of the available choices."]
    }


# --- invoices -------------------------------------------------------------------------------------


def test_generate_invoice(finance_api, fee_setup, fall_enrollment, assert_matches_openapi):
    r = finance_api.post(
        "/api/v1/invoices/generate/",
        {
            "enrollment_id": fall_enrollment.enrollment_id,
            "waivers": [{"fee_type": "TECHNOLOGY", "amount": "1500.00", "reason": "HARDSHIP"}],
        },
        format="json",
    )
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/invoices/generate/", "post")
    body = r.json()
    assert body["invoice_id"] == INV
    assert (body["lifecycle_status"], body["collection_status"]) == ("ISSUED", "UNPAID")
    assert body["total_amount"] == "162500.00"
    assert body["amounts"] == {
        "original_charges": "162500.00",
        "adjustments": "0.00",
        "net_charges": "162500.00",
        "scholarship_applied": "0.00",
        "amount_paid": "0.00",
        "payments_by_payer": {"SELF": "0.00", "GUARDIAN": "0.00", "SPONSOR": "0.00"},
        "outstanding": "162500.00",
        "is_settled": False,
    }
    tech = next(x for x in body["lines"] if x["fee_type"] == "TECHNOLOGY")
    assert (
        tech["gross_amount"],
        tech["waiver_amount"],
        tech["net_amount"],
        tech["open_amount"],
    ) == (
        "4000.00",
        "1500.00",
        "2500.00",
        "2500.00",
    )
    assert tech["quantity"] == "1.00" and body["lines"][0]["quantity"] == "16.00"


def test_generate_twice_is_409(finance_api, invoice, fall_enrollment):
    r = finance_api.post(
        "/api/v1/invoices/generate/",
        {"enrollment_id": fall_enrollment.enrollment_id},
        format="json",
    )
    e = _error(r, 409, "DUPLICATE_INVOICE")
    assert e["rule_id"] == "BR-INV-01" and e["details"]["existing_invoice_id"] == INV


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "enrollment_id"),
        ({"enrollment_id": "ENR-2026-999999"}, "enrollment_id"),
        (
            {
                "enrollment_id": "ENR-2026-000001",
                "waivers": [{"fee_type": "LIBRARY", "amount": 100}],
            },
            "waivers",
        ),
        (
            {
                "enrollment_id": "ENR-2026-000001",
                "waivers": [{"fee_type": "LIBRARY", "amount": "1", "x": 1}],
            },
            "waivers",
        ),
        ({"enrollment_id": "ENR-2026-000001", "waivers": "LIBRARY"}, "waivers"),
        ({"enrollment_id": "ENR-2026-000001", "actor": "me"}, "actor"),
    ],
)
def test_generate_validation(finance_api, fee_setup, fall_enrollment, body, field):
    r = finance_api.post("/api/v1/invoices/generate/", body, format="json")
    assert field in _error(r, 400, "VALIDATION_ERROR")["details"]


def test_generate_for_completed_enrollment_is_409(finance_api, fee_setup, fall_enrollment):
    from academics import services as academics

    academics.complete_enrollment(fall_enrollment, on_date=date(2026, 12, 20), actor="t")
    r = finance_api.post(
        "/api/v1/invoices/generate/",
        {"enrollment_id": fall_enrollment.enrollment_id},
        format="json",
    )
    assert _error(r, 409, "ILLEGAL_TRANSITION")["details"]["status"] == "COMPLETED"


def test_generate_without_fee_schedules_is_422(finance_api, fall_enrollment):
    r = finance_api.post(
        "/api/v1/invoices/generate/",
        {"enrollment_id": fall_enrollment.enrollment_id},
        format="json",
    )
    assert _error(r, 422, "FEE_SCHEDULE_NOT_FOUND")["details"]["missing_fee_types"] == [
        "TUITION",
        "LAB",
        "REGISTRATION",
    ]


def test_invoice_list_detail_and_filters(finance_api, invoice, assert_matches_openapi):
    r = finance_api.get("/api/v1/invoices/")
    assert r.status_code == 200
    assert_matches_openapi(r, "/api/v1/invoices/", "get")
    assert [i["invoice_id"] for i in r.json()["results"]] == [INV]
    assert "lines" not in r.json()["results"][0]
    for params, n in [
        ({"student": "STU-000001"}, 1),
        ({"student": "STU-000002"}, 0),
        ({"term": "TERM-2026-FALL"}, 1),
        ({"enrollment": "ENR-2026-000001"}, 1),
        ({"lifecycle_status": "ISSUED", "collection_status": "UNPAID"}, 1),
        ({"collection_status": "OVERDUE"}, 0),
    ]:
        assert finance_api.get("/api/v1/invoices/", params).json()["count"] == n, params
    r = finance_api.get(f"/api/v1/invoices/{INV}/")
    assert_matches_openapi(r, "/api/v1/invoices/{invoice_id}/", "get")
    assert [x["line_id"][-3:] for x in r.json()["lines"]] == [
        "L01",
        "L02",
        "L03",
        "L04",
        "L05",
        "L06",
    ]
    r = finance_api.get("/api/v1/invoices/", {"collection_status": "LATE"})
    assert _error(r, 400, "VALIDATION_ERROR")["details"] == {
        "collection_status": ["Select a valid choice. LATE is not one of the available choices."]
    }
    assert _error(finance_api.get("/api/v1/invoices/INV-NOPE/"), 404, "NOT_FOUND")["message"] == (
        "No Invoice matches the given query."
    )


def test_void_invoice(finance_api, invoice, assert_matches_openapi):
    r = finance_api.post(
        f"/api/v1/invoices/{INV}/void/", {"void_date": "2026-08-20"}, format="json"
    )
    assert r.status_code == 200, r.content
    assert_matches_openapi(r, "/api/v1/invoices/{invoice_id}/void/", "post")
    body = r.json()
    assert (body["lifecycle_status"], body["collection_status"]) == ("VOID", "ADJUSTED_TO_ZERO")
    assert (
        body["amounts"]["net_charges"] == "0.00" and body["amounts"]["adjustments"] == "-164000.00"
    )
    adj = [x for x in body["lines"] if x["line_type"] == "ADJUSTMENT"]
    assert len(adj) == 6 and all(
        x["open_amount"] is None and x["adjustment_reason"] == "VOID" for x in adj
    )
    assert all(x["open_amount"] == "0.00" for x in body["lines"] if x["line_type"] == "FEE")
    r = finance_api.post(
        f"/api/v1/invoices/{INV}/void/", {"void_date": "2026-08-21"}, format="json"
    )
    assert _error(r, 409, "ILLEGAL_TRANSITION")["rule_id"] == "BR-INV-08"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "This field is required."),
        (
            {"void_date": "2026-13-01"},
            "Date has wrong format. Use one of these formats instead: YYYY-MM-DD.",
        ),
        (
            {"void_date": "2026-08-16"},
            "void_date must be on or after the invoice issue date (2026-08-17).",
        ),
        ({"void_date": "2027-01-04"}, "Must be a business date between 2026-01-01 and 2026-12-31."),
    ],
)
def test_D15_void_date_validation(finance_api, invoice, body, message):
    r = finance_api.post(f"/api/v1/invoices/{INV}/void/", body, format="json")
    assert _error(r, 400, "VALIDATION_ERROR")["details"] == {"void_date": [message]}


def test_void_unknown_invoice_is_404(finance_api, invoice):
    r = finance_api.post(
        "/api/v1/invoices/INV-STU-000001-2026-999999/void/",
        {"void_date": "2026-08-20"},
        format="json",
    )
    assert _error(r, 404, "NOT_FOUND")["message"] == "No Invoice matches the given query."


# --- batch operations ---------------------------------------------------------------------------


def test_late_fee_assess_dry_run_then_real(finance_api, invoice, assert_matches_openapi):
    r = finance_api.post(
        "/api/v1/billing/late-fees/assess/",
        {"as_of_date": "2026-09-29", "dry_run": True},
        format="json",
    )
    assert r.status_code == 200, r.content
    assert_matches_openapi(r, "/api/v1/billing/late-fees/assess/", "post")
    assert r.json() == {
        "as_of_date": "2026-09-29",
        "dry_run": True,
        "examined": 1,
        "assessed_count": 1,
        "total_late_fees": "3280.00",
        "assessed": [
            {
                "invoice_id": INV,
                "outstanding": "164000.00",
                "late_fee": "3280.00",
                "posted_date": "2026-09-29",
                "line_id": None,
            }
        ],
    }
    r = finance_api.post(
        "/api/v1/billing/late-fees/assess/", {"as_of_date": "2026-09-29"}, format="json"
    )
    assert r.json()["dry_run"] is False and r.json()["assessed"][0]["line_id"] == f"{INV}-L07"
    detail = finance_api.get(f"/api/v1/invoices/{INV}/").json()
    assert (
        detail["collection_status"] == "OVERDUE"
        and detail["amounts"]["original_charges"] == "167280.00"
    )


@pytest.mark.parametrize(
    ("path", "body", "details"),
    [
        ("/api/v1/billing/late-fees/assess/", {}, {"as_of_date": ["This field is required."]}),
        (
            "/api/v1/billing/late-fees/assess/",
            {"as_of_date": "2026-09-29", "x": 1},
            {"x": ["Unknown field."]},
        ),
        (
            "/api/v1/billing/late-fees/assess/",
            {"as_of_date": "2027-01-01"},
            {"as_of_date": ["Must be a business date between 2026-01-01 and 2026-12-31."]},
        ),
        (
            "/api/v1/billing/collection-status/refresh/",
            {},
            {"as_of_date": ["This field is required."]},
        ),
        (
            "/api/v1/billing/collection-status/refresh/",
            {"as_of_date": "2025-12-31"},
            {"as_of_date": ["Must be a business date between 2026-01-01 and 2026-12-31."]},
        ),
    ],
)
def test_D8_batch_dates_are_required_business_dates(finance_api, path, body, details):
    assert (
        _error(finance_api.post(path, body, format="json"), 400, "VALIDATION_ERROR")["details"]
        == details
    )


def test_collection_refresh(finance_api, invoice, assert_matches_openapi):
    r = finance_api.post(
        "/api/v1/billing/collection-status/refresh/", {"as_of_date": "2026-09-15"}, format="json"
    )
    assert r.status_code == 200, r.content
    assert_matches_openapi(r, "/api/v1/billing/collection-status/refresh/", "post")
    assert r.json() == {
        "as_of_date": "2026-09-15",
        "examined": 1,
        "changed": 1,
        "unchanged": 0,
        "skipped_later_as_of": 0,
        "changes": [{"invoice_id": INV, "from_status": "UNPAID", "to_status": "OVERDUE"}],
    }


def test_batch_endpoints_are_post_only(finance_api):
    for path in ("/api/v1/billing/late-fees/assess/", "/api/v1/billing/collection-status/refresh/"):
        r = finance_api.get(path)
        assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


# --- student account views ------------------------------------------------------------------------


def test_student_ledger(finance_api, invoice, assert_matches_openapi):
    r = finance_api.get("/api/v1/students/STU-000001/ledger/")
    assert r.status_code == 200
    assert_matches_openapi(r, "/api/v1/students/{student_id}/ledger/", "get")
    rows = r.json()["results"]
    assert r.json()["count"] == 6
    assert rows[0] == {
        "entry_id": "LED-2026-0000001",
        "student": "STU-000001",
        "entry_date": "2026-08-17",
        "entry_type": "CHARGE",
        "amount": "144000.00",
        "source_type": "INVOICE_LINE",
        "invoice_line": f"{INV}-L01",
        "scholarship_allocation": None,  # Phase 3: the second ledger source
        "payment": None,  # Phase 4: the third and fourth ledger sources
        "refund": None,
        "invoice": INV,
        "memo": "Tuition (16 credits)",
    }


def test_student_statement(finance_api, invoice, assert_matches_openapi):
    r = finance_api.get("/api/v1/students/STU-000001/statement/")
    assert r.status_code == 200
    assert_matches_openapi(r, "/api/v1/students/{student_id}/statement/", "get")
    body = r.json()
    assert body["student_id"] == "STU-000001" and body["refunds"] == []
    (inv,) = body["invoices"]
    assert inv["invoice_id"] == INV and len(inv["lines"]) == 6
    assert inv["scholarship_allocations"] == [] and inv["payment_allocations"] == []


def test_student_balance(finance_api, invoice, assert_matches_openapi):
    r = finance_api.get("/api/v1/students/STU-000001/balance/")
    assert r.status_code == 200
    assert_matches_openapi(r, "/api/v1/students/{student_id}/balance/", "get")
    assert r.json() == {
        "student_id": "STU-000001",
        "as_of": None,
        "ledger_balance": "164000.00",
        "breakdown": {
            "charges": "164000.00",
            "late_fees": "0.00",
            "adjustments": "0.00",
            "scholarship_credits": "0.00",
            "scholarship_reversals": "0.00",
            "payments": "0.00",
            "payments_by_payer": {"SELF": "0.00", "GUARDIAN": "0.00", "SPONSOR": "0.00"},
            "refunds": "0.00",
        },
        "invoice_outstanding": "164000.00",
        "unallocated_credit": "0.00",
        "identity": {
            "rule_id": "BR-BAL-02",
            "holds": True,
            "ledger_balance": "164000.00",
            "invoice_outstanding_minus_unallocated_credit": "164000.00",
        },
    }
    r = finance_api.get("/api/v1/students/STU-000001/balance/", {"as_of": "2026-08-16"})
    assert r.json()["as_of"] == "2026-08-16" and r.json()["ledger_balance"] == "0.00"


@pytest.mark.parametrize(
    ("params", "details"),
    [
        (
            {"as_of": "16-08-2026"},
            {"as_of": ["Date has wrong format. Use one of these formats instead: YYYY-MM-DD."]},
        ),
        (
            {"as_of": "2027-01-01"},
            {"as_of": ["Must be a business date between 2026-01-01 and 2026-12-31."]},
        ),
    ],
)
def test_student_balance_validation(finance_api, invoice, params, details):
    r = finance_api.get("/api/v1/students/STU-000001/balance/", params)
    assert _error(r, 400, "VALIDATION_ERROR")["details"] == details


@pytest.mark.parametrize("view", ["ledger", "statement", "balance"])
def test_student_views_404_for_unknown_student(finance_api, view):
    r = finance_api.get(f"/api/v1/students/STU-999999/{view}/")
    assert _error(r, 404, "NOT_FOUND")["message"] == "No Student matches the given query."


@pytest.mark.parametrize("view", ["ledger", "statement", "balance"])
def test_student_views_for_student_without_invoices(finance_api, student, view):
    r = finance_api.get(f"/api/v1/students/{student.student_id}/{view}/")
    assert r.status_code == 200


# --- money and methods ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", f"/api/v1/invoices/{INV}/"),
        ("patch", f"/api/v1/invoices/{INV}/"),
        ("delete", f"/api/v1/invoices/{INV}/"),
        ("put", "/api/v1/fee-schedules/"),
        ("delete", "/api/v1/fee-schedules/"),
        ("put", "/api/v1/students/STU-000001/ledger/"),
        ("delete", "/api/v1/students/STU-000001/balance/"),
    ],
)
def test_financial_records_are_read_and_action_only(finance_api, invoice, method, path):
    r = getattr(finance_api, method)(path, {}, format="json")
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_money_is_always_a_string_in_responses(finance_api, invoice):
    def walk(node, key=None):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, k)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v, key)
        else:
            yield key, node

    bodies = [
        finance_api.get(p).json()
        for p in (
            f"/api/v1/invoices/{INV}/",
            "/api/v1/students/STU-000001/balance/",
            "/api/v1/students/STU-000001/ledger/",
            "/api/v1/fee-schedules/",
        )
    ]
    numbers = [(k, v) for b in bodies for k, v in walk(b) if isinstance(v, float)]
    assert numbers == []
