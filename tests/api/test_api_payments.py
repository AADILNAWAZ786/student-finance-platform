"""Payments and refunds API: every endpoint, filters, strict input, permissions (D-P3) and
error envelopes; every success response validated against the served OpenAPI document.

STU-000001's FALL invoice (issued 2026-08-17): L01 TUITION 144,000, L02 LAB 6,000,
L03 REGISTRATION 5,000, L04 TECHNOLOGY 4,000, L05 LIBRARY 2,000, L06 ACTIVITY 3,000."""

from datetime import date

import pytest

pytestmark = pytest.mark.django_db

INV = "INV-STU-000001-2026-000001"
PAY1, PAY2 = "PAY-2026-000001", "PAY-2026-000002"
REF1 = "REF-2026-000001"


def _body(**kw):
    body = {
        "student_id": "STU-000001",
        "amount": "60000.00",
        "received_date": "2026-08-25",
        "method": "BANK_TRANSFER",
        "payer_type": "GUARDIAN",
        "payer_name": "R. Rao",
        "external_reference": "NEFT-0825-001",
    }
    body.update(kw)
    return body


def _error(r, status, code):
    assert r.status_code == status, r.content
    assert r.json()["error"]["code"] == code
    return r.json()["error"]


@pytest.fixture
def world(invoice, finance_api):
    return finance_api


# --- payments ---------------------------------------------------------------------------------


def test_record_payment_with_auto_allocation(world, assert_matches_openapi):
    r = world.post("/api/v1/payments/", _body(auto_allocate=True), format="json")
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/payments/", "post")
    body = r.json()
    assert {k: body[k] for k in ("payment_id", "amount", "status", "payer_type")} == {
        "payment_id": PAY1,
        "amount": "60000.00",
        "status": "POSTED",
        "payer_type": "GUARDIAN",
    }
    assert (body["net_allocated"], body["available"], body["allocation_state"]) == (
        "60000.00",
        "0.00",
        "FULL",
    )
    auto = body["auto_allocation"]
    assert (auto["mode"], auto["outcome"], auto["allocated"], auto["unallocated_after"]) == (
        "AUTO",
        "ALLOCATED",
        "60000.00",
        "0.00",
    )
    assert [(a["invoice_line"], a["amount"], a["rule_applied"]) for a in auto["allocations"]] == [
        (f"{INV}-L01", "60000.00", "AUTO_OLDEST_FIRST")
    ]
    inv = world.get(f"/api/v1/invoices/{INV}/").json()
    assert (inv["amounts"]["amount_paid"], inv["collection_status"]) == (
        "60000.00",
        "PARTIALLY_PAID",
    )
    assert inv["amounts"]["payments_by_payer"]["GUARDIAN"] == "60000.00"


def test_record_payment_without_allocation_then_read(world, assert_matches_openapi):
    r = world.post("/api/v1/payments/", _body(), format="json")
    assert r.status_code == 201 and r.json()["auto_allocation"] is None
    assert r.json()["allocation_state"] == "UNALLOCATED"
    r = world.get(f"/api/v1/payments/{PAY1}/")
    assert_matches_openapi(r, "/api/v1/payments/{payment_id}/", "get")
    assert (r.json()["available"], r.json()["allocations"]) == ("60000.00", [])
    r = world.get("/api/v1/payments/", {"student": "STU-000001", "payer_type": "GUARDIAN"})
    assert_matches_openapi(r, "/api/v1/payments/", "get")
    assert [p["payment_id"] for p in r.json()["results"]] == [PAY1]
    assert world.get("/api/v1/payments/", {"payer_type": "SPONSOR"}).json()["count"] == 0
    assert world.get("/api/v1/payments/", {"received_to": "2026-08-24"}).json()["count"] == 0


def test_duplicate_payment_is_409(world):
    world.post("/api/v1/payments/", _body(), format="json")
    err = _error(
        world.post("/api/v1/payments/", _body(amount="1.00"), format="json"),
        409,
        "DUPLICATE_PAYMENT",
    )
    assert err["rule_id"] == "BR-PAY-01"


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"amount": 100}, "amount"),  # money must be a string
        ({"amount": "1.001"}, "amount"),
        ({"amount": "-1.00"}, "amount"),
        ({"method": "CRYPTO"}, "method"),
        ({"payer_type": "PARENT"}, "payer_type"),
        ({"student_id": "STU-999999"}, "student_id"),
        ({"status": "POSTED"}, "status"),  # server-controlled: unknown field
        ({"currency": "INR"}, "currency"),
        ({"received_date": "2027-01-01"}, "received_date"),
        ({"external_reference": "x" * 41}, "external_reference"),
    ],
)
def test_payment_validation(world, patch, field):
    err = _error(
        world.post("/api/v1/payments/", _body(**patch), format="json"), 400, "VALIDATION_ERROR"
    )
    assert list(err["details"]) == [field]


def test_allocate_auto_and_manual(world, assert_matches_openapi):
    world.post("/api/v1/payments/", _body(amount="10000.00"), format="json")
    r = world.post(
        f"/api/v1/payments/{PAY1}/allocate/",
        {
            "mode": "MANUAL",
            "allocated_on": "2026-08-26",
            "lines": [{"invoice_line_id": f"{INV}-L02", "amount": "6000.00"}],
        },
        format="json",
    )
    assert r.status_code == 200, r.content
    assert_matches_openapi(r, "/api/v1/payments/{payment_id}/allocate/", "post")
    assert (r.json()["outcome"], r.json()["payment"]["available"]) == ("ALLOCATED", "4000.00")
    r = world.post(f"/api/v1/payments/{PAY1}/allocate/", {"mode": "AUTO"}, format="json")
    assert [
        (a["invoice_line"], a["amount"], a["allocated_on"]) for a in r.json()["allocations"]
    ] == [
        (f"{INV}-L01", "4000.00", "2026-08-25")  # AUTO: dated received_date (D-P6)
    ]
    r = world.post(f"/api/v1/payments/{PAY1}/allocate/", {"mode": "AUTO"}, format="json")
    assert r.status_code == 200
    assert (r.json()["outcome"], r.json()["allocations"]) == ("NO_FUNDS_AVAILABLE", [])
    r = world.get(f"/api/v1/payments/{PAY1}/allocations/")
    assert_matches_openapi(r, "/api/v1/payments/{payment_id}/allocations/", "get")
    assert [a["allocation_id"] for a in r.json()["results"]] == [
        "ALLOC-PAY-2026-000001-01",
        "ALLOC-PAY-2026-000001-02",
    ]
    r = world.get("/api/v1/payment-allocations/", {"invoice_line": f"{INV}-L02"})
    assert_matches_openapi(r, "/api/v1/payment-allocations/", "get")
    assert r.json()["count"] == 1 and r.json()["results"][0]["payer_type"] == "GUARDIAN"
    assert world.get("/api/v1/payment-allocations/", {"invoice": INV}).json()["count"] == 2
    assert (
        world.get("/api/v1/payment-allocations/", {"allocation_type": "DEALLOCATE"}).json()["count"]
        == 0
    )


def test_auto_with_no_open_lines_is_200_and_keeps_the_credit(world):
    world.post("/api/v1/payments/", _body(amount="164000.00", auto_allocate=True), format="json")
    r = world.post(
        "/api/v1/payments/",
        _body(amount="500.00", external_reference="EXTRA", auto_allocate=True),
        format="json",
    )
    assert r.status_code == 201
    auto = r.json()["auto_allocation"]
    assert (auto["outcome"], auto["allocations"], auto["unallocated_after"]) == (
        "NO_OPEN_LINES",
        [],
        "500.00",
    )
    balance = world.get("/api/v1/students/STU-000001/balance/").json()
    assert (balance["ledger_balance"], balance["unallocated_credit"]) == ("-500.00", "500.00")
    assert balance["identity"]["holds"] is True


@pytest.mark.parametrize(
    ("body", "errors"),
    [
        ({"mode": "AUTO", "lines": []}, {"lines": ["Must be omitted when mode is AUTO."]}),
        (
            {"mode": "MANUAL"},
            {
                "lines": ["Required (1–99 lines) when mode is MANUAL."],
                "allocated_on": ["Required when mode is MANUAL."],
            },
        ),
        ({"mode": "SPLIT"}, {"mode": ['"SPLIT" is not a valid choice.']}),
        (
            {
                "mode": "MANUAL",
                "allocated_on": "2026-08-26",
                "lines": [{"invoice_line_id": f"{INV}-L01", "amount": "1.00", "x": 1}],
            },
            {"lines": {"0": {"x": ["Unknown field."]}}},  # DRF keys nested errors by index
        ),
    ],
)
def test_allocate_validation(world, body, errors):
    world.post("/api/v1/payments/", _body(), format="json")
    err = _error(
        world.post(f"/api/v1/payments/{PAY1}/allocate/", body, format="json"),
        400,
        "VALIDATION_ERROR",
    )
    assert err["details"] == errors


def test_allocate_business_rule_errors(world):
    world.post("/api/v1/payments/", _body(amount="1000.00"), format="json")

    def manual(line, amount):
        return world.post(
            f"/api/v1/payments/{PAY1}/allocate/",
            {
                "mode": "MANUAL",
                "allocated_on": "2026-08-26",
                "lines": [{"invoice_line_id": line, "amount": amount}],
            },
            format="json",
        )

    assert (
        _error(manual(f"{INV}-L01", "1000.01"), 422, "ALLOCATION_EXCEEDS_PAYMENT")["rule_id"]
        == "BR-ALC-01"
    )
    world.post("/api/v1/payments/", _body(amount="6000.00", external_reference="B"), format="json")
    world.post(
        f"/api/v1/payments/{PAY2}/allocate/",
        {
            "mode": "MANUAL",
            "allocated_on": "2026-08-26",
            "lines": [{"invoice_line_id": f"{INV}-L05", "amount": "1500.00"}],
        },
        format="json",
    )
    assert (
        _error(manual(f"{INV}-L05", "500.01"), 422, "ALLOCATION_EXCEEDS_LINE_OPEN")["rule_id"]
        == "BR-ALC-02"
    )
    assert _error(manual(f"{INV}-L99", "1.00"), 400, "VALIDATION_ERROR")["details"]["lines"]


# --- refunds -------------------------------------------------------------------------------------


@pytest.fixture
def overpaid(world):
    """174,000 received, AUTO: 164,000 allocated, 10,000 unallocated (credit 10,000)."""
    r = world.post(
        "/api/v1/payments/",
        _body(amount="174000.00", payer_type="SPONSOR", auto_allocate=True),
        format="json",
    )
    assert r.status_code == 201
    return world


def test_refundable_then_request_approve_process(overpaid, assert_matches_openapi):
    api = overpaid
    r = api.get("/api/v1/students/STU-000001/refundable/")
    assert_matches_openapi(r, "/api/v1/students/{student_id}/refundable/", "get")
    assert r.json() == {
        "student_id": "STU-000001",
        "max_refundable": "10000.00",
        "calculation": {
            "net_paid": "174000.00",
            "ledger_balance": "-10000.00",
            "credit_balance": "10000.00",
            "pending_refunds": "0.00",
            "credit_available": "10000.00",
            "payments_available": "10000.00",
        },
        "payments": [
            {
                "payment_id": PAY1,
                "payer_type": "SPONSOR",
                "received_date": "2026-08-25",
                "amount": "174000.00",
                "net_allocated": "164000.00",
                "refunds_reserved_or_processed": "0.00",
                "available": "10000.00",
                "max_refundable": "10000.00",
            }
        ],
    }
    r = api.post(
        "/api/v1/refunds/",
        {"payment_id": PAY1, "amount": "10000.00", "requested_date": "2026-09-18"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/refunds/", "post")
    assert (r.json()["status"], r.json()["payer_type"], r.json()["reason"]) == (
        "REQUESTED",
        "SPONSOR",
        "OVERPAYMENT",
    )
    snapshot = r.json()["calculation"]
    assert api.get("/api/v1/students/STU-000001/refundable/").json()["max_refundable"] == "0.00"
    r = api.post(f"/api/v1/refunds/{REF1}/approve/", {"on_date": "2026-09-20"}, format="json")
    assert_matches_openapi(r, "/api/v1/refunds/{refund_id}/approve/", "post")
    assert r.json()["status"] == "APPROVED"
    r = api.post(
        f"/api/v1/refunds/{REF1}/process/", {"processed_date": "2026-09-30"}, format="json"
    )
    assert_matches_openapi(r, "/api/v1/refunds/{refund_id}/process/", "post")
    assert (r.json()["status"], r.json()["processed_date"], r.json()["calculation"]) == (
        "PROCESSED",
        "2026-09-30",
        snapshot,
    )
    assert [e["to_status"] for e in r.json()["events"]] == ["REQUESTED", "APPROVED", "PROCESSED"]
    r = api.get(f"/api/v1/refunds/{REF1}/")
    assert_matches_openapi(r, "/api/v1/refunds/{refund_id}/", "get")
    r = api.get("/api/v1/refunds/", {"status": "PROCESSED", "student": "STU-000001"})
    assert_matches_openapi(r, "/api/v1/refunds/", "get")
    assert r.json()["count"] == 1
    # the ledger, statement and balance now show the payment and the refund
    ledger = api.get("/api/v1/students/STU-000001/ledger/").json()["results"]
    sources = [
        (e["entry_type"], e["source_type"], e["payment"], e["refund"], e["amount"])
        for e in ledger[-2:]
    ]
    assert sources == [
        ("PAYMENT", "PAYMENT", PAY1, None, "-174000.00"),
        ("REFUND", "REFUND", None, REF1, "10000.00"),
    ]
    statement = api.get("/api/v1/students/STU-000001/statement/").json()
    assert [r["refund_id"] for r in statement["refunds"]] == [REF1]
    assert len(statement["invoices"][0]["payment_allocations"]) == 6
    balance = api.get("/api/v1/students/STU-000001/balance/").json()
    assert balance["breakdown"]["refunds"] == "10000.00"
    assert balance["breakdown"]["payments_by_payer"]["SPONSOR"] == "-174000.00"
    assert (balance["ledger_balance"], balance["identity"]["holds"]) == ("0.00", True)


def test_reject_releases_the_reservation(overpaid):
    api = overpaid
    api.post(
        "/api/v1/refunds/",
        {"payment_id": PAY1, "amount": "10000.00", "requested_date": "2026-09-18"},
        format="json",
    )
    r = api.post(f"/api/v1/refunds/{REF1}/reject/", {"on_date": "2026-09-19"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert api.get("/api/v1/students/STU-000001/refundable/").json()["max_refundable"] == "10000.00"


def test_refund_errors(overpaid):
    api = overpaid

    def request(amount, **kw):
        return api.post(
            "/api/v1/refunds/",
            {"payment_id": PAY1, "amount": amount, "requested_date": "2026-09-18", **kw},
            format="json",
        )

    assert (
        _error(request("10000.01"), 422, "REFUND_EXCEEDS_CREDIT_BALANCE")["rule_id"] == "BR-REF-02"
    )
    # Phase 5 (D-W6): WITHDRAWAL is a valid reason, but needs a withdrawn enrollment
    assert _error(request("1.00", reason="WITHDRAWAL"), 400, "VALIDATION_ERROR")["details"] == {
        "enrollment_id": ["Required when reason is WITHDRAWAL."]
    }
    assert _error(request("1.00", reason="GOODWILL"), 400, "VALIDATION_ERROR")["details"] == {
        "reason": ['"GOODWILL" is not a valid choice.']
    }
    assert list(_error(request("1.00", status="APPROVED"), 400, "VALIDATION_ERROR")["details"]) == [
        "status"
    ]
    assert list(_error(request("1.00", calculation={}), 400, "VALIDATION_ERROR")["details"]) == [
        "calculation"
    ]
    assert list(
        _error(request("1.00", requested_date="2026-08-24"), 400, "VALIDATION_ERROR")["details"]
    ) == ["requested_date"]
    request("1.00")
    r = api.post(
        f"/api/v1/refunds/{REF1}/process/", {"processed_date": "2026-09-20"}, format="json"
    )
    assert _error(r, 409, "ILLEGAL_TRANSITION")["rule_id"] == "BR-REF-03"


# --- permissions (ARCHITECTURE §13, D-P3) ---------------------------------------------------------


def test_support_staff_reads_everything_and_may_request_a_refund(overpaid, staff_api):
    for path in (
        "/api/v1/payments/",
        f"/api/v1/payments/{PAY1}/",
        f"/api/v1/payments/{PAY1}/allocations/",
        "/api/v1/payment-allocations/",
        "/api/v1/refunds/",
        "/api/v1/students/STU-000001/refundable/",
    ):
        assert staff_api.get(path).status_code == 200, path
    r = staff_api.post(
        "/api/v1/refunds/",
        {"payment_id": PAY1, "amount": "100.00", "requested_date": "2026-09-18"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.json()["events"][0]["actor"].startswith("support_staff")
    for path, body in (
        (f"/api/v1/refunds/{REF1}/approve/", {"on_date": "2026-09-19"}),
        (f"/api/v1/refunds/{REF1}/process/", {"processed_date": "2026-09-19"}),
        (f"/api/v1/refunds/{REF1}/reject/", {"on_date": "2026-09-19"}),
        ("/api/v1/payments/", _body(external_reference="STAFF")),
        (f"/api/v1/payments/{PAY1}/allocate/", {"mode": "AUTO"}),
    ):
        assert _error(staff_api.post(path, body, format="json"), 403, "PERMISSION_DENIED")


@pytest.mark.parametrize("role", ["learner", "instructor", "academic_admin"])
def test_other_roles_have_no_access_to_payments(overpaid, api_client_for, role):
    api = api_client_for(role)
    for path in (
        "/api/v1/payments/",
        "/api/v1/refunds/",
        "/api/v1/students/STU-000001/refundable/",
    ):
        assert api.get(path).status_code == 403, (role, path)
    r = api.post(
        "/api/v1/refunds/",
        {"payment_id": PAY1, "amount": "1.00", "requested_date": "2026-09-18"},
        format="json",
    )
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", f"/api/v1/payments/{PAY1}/"),
        ("patch", f"/api/v1/payments/{PAY1}/"),
        ("delete", f"/api/v1/payments/{PAY1}/"),
        ("patch", f"/api/v1/refunds/{REF1}/"),
        ("delete", f"/api/v1/refunds/{REF1}/"),
        ("delete", "/api/v1/payment-allocations/"),
    ],
)
def test_financial_records_are_read_and_action_only(overpaid, method, path):
    assert _error(getattr(overpaid, method)(path, {}, format="json"), 405, "METHOD_NOT_ALLOWED")


def test_money_is_always_a_string(overpaid):
    def floats(node):
        if isinstance(node, dict):
            return [x for v in node.values() for x in floats(v)]
        if isinstance(node, list):
            return [x for v in node for x in floats(v)]
        return [node] if isinstance(node, float) else []

    bodies = [
        overpaid.get(p).json()
        for p in (
            "/api/v1/payments/",
            f"/api/v1/payments/{PAY1}/",
            "/api/v1/payment-allocations/",
            "/api/v1/students/STU-000001/refundable/",
        )
    ]
    assert [x for b in bodies for x in floats(b)] == []


def test_business_dates_never_come_from_the_clock(overpaid):
    """Every payments business date is sent or derived from stored dates; no clock in the
    app. (``recorded_at`` is execution metadata and does use the clock, by design.)"""
    from pathlib import Path

    source = "\n".join(
        p.read_text() for p in Path("payments").rglob("*.py") if "migrations" not in p.parts
    )
    assert not any(
        w in source
        for w in ("business_today", "date.today", "localdate", "timezone.now", "datetime.now")
    )
    r = overpaid.get(f"/api/v1/payments/{PAY1}/allocations/").json()["results"]
    assert {a["allocated_on"] for a in r} == {date(2026, 8, 25).isoformat()}
