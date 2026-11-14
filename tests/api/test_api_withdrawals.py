"""POST /enrollments/{id}/withdraw/ and /cancel/ (Phase 5): responses validated against the
served OpenAPI document, D-W5 visibility (financial details for platform_admin only), roles,
strict input, and refunds requested afterwards (D-W6).

STU-000001's FALL invoice (enrolled 2026-08-10, issued 08-17; start 09-07): L01 TUITION
144,000, L02 LAB 6,000, L03 REGISTRATION 5,000, L04 TECHNOLOGY 4,000, L05 LIBRARY 2,000,
L06 STUDENT_ACTIVITY 3,000."""

import json

import pytest

from academics.models import Enrollment
from billing.models import InvoiceLine, LedgerEntry
from payments.models import PaymentAllocation, Refund

pytestmark = pytest.mark.django_db

FINANCIAL_KEYS = {"invoice", "refund_quote"}


@pytest.fixture
def enr(invoice):
    return invoice.enrollment_id


@pytest.fixture
def paid(invoice, record, merit_award):
    """Merit 75,000 on tuition; a guardian payment of 89,000 AUTO-allocated (fully paid)."""
    from datetime import date

    from scholarships import services as scholarships

    scholarships.apply_scholarship(merit_award, actor="t")
    return record("89000.00", payer="GUARDIAN", on=date(2026, 8, 20), auto=True)


def _snapshot():
    return (
        list(Enrollment.objects.values_list("pk", "status")),
        InvoiceLine.objects.count(),
        LedgerEntry.objects.count(),
        PaymentAllocation.objects.count(),
    )


def test_platform_admin_gets_the_release_plan_and_quote(
    finance_api, enr, paid, assert_matches_openapi
):
    path = f"/api/v1/enrollments/{enr}/withdraw/"
    r = finance_api.post(path, {"withdrawal_date": "2026-09-17"}, format="json")
    assert r.status_code == 200, r.content
    assert_matches_openapi(r, "/api/v1/enrollments/{enrollment_id}/withdraw/", "post")
    body = r.json()
    assert (body["kind"], body["refund_pct"], body["financial_details_included"]) == (
        "WITHDRAWAL",
        "0.60",
        True,
    )
    assert (body["enrollment"]["status"], body["enrollment"]["withdrawal_date"]) == (
        "WITHDRAWN",
        "2026-09-17",
    )
    inv = body["invoice"]
    assert (inv["lifecycle_status"], inv["collection_status"], inv["amounts"]["outstanding"]) == (
        "ISSUED",
        "PAID",
        "0.00",
    )
    l01 = inv["release_plan"][0]
    assert {
        k: l01[k]
        for k in (
            "line_net",
            "adjustment",
            "reduced_net",
            "scholarship_allocated",
            "scholarship_reversal",
            "payments_allocated",
            "excess",
        )
    } == {
        "line_net": "144000.00",
        "adjustment": "-86400.00",
        "reduced_net": "57600.00",
        "scholarship_allocated": "75000.00",
        "scholarship_reversal": "45000.00",
        "payments_allocated": "69000.00",
        "excess": "41400.00",
    }
    assert [d["amount"] for d in l01["deallocations"]] == ["41400.00"]
    assert body["refund_quote"]["max_refundable"] == "45000.00"
    assert Refund.objects.count() == 0  # D-W6: a quote, never a refund


def test_academic_admin_gets_no_financial_details(
    api_client_for, enr, paid, assert_matches_openapi
):
    api = api_client_for("academic_admin")
    r = api.post(
        f"/api/v1/enrollments/{enr}/withdraw/", {"withdrawal_date": "2026-09-17"}, format="json"
    )
    assert r.status_code == 200, r.content
    assert_matches_openapi(r, "/api/v1/enrollments/{enrollment_id}/withdraw/", "post")
    body = r.json()
    assert set(body) == {
        "enrollment",
        "kind",
        "effective_date",
        "refund_pct",
        "financial_details_included",
    }
    assert body["financial_details_included"] is False
    text = json.dumps(body)
    for word in (
        "amount",
        "outstanding",
        "release_plan",
        "refund_quote",
        "invoice",
        "allocation",
        "max_refundable",
    ):
        assert word not in text, word
    # the withdrawal itself was complete: financial effects happened, just not shown
    assert PaymentAllocation.objects.filter(allocation_type="DEALLOCATE").count() == 2


def test_academic_admin_cancels_without_financial_details(api_client_for, enr, invoice):
    api = api_client_for("academic_admin")
    r = api.post(
        f"/api/v1/enrollments/{enr}/cancel/", {"cancellation_date": "2026-08-20"}, format="json"
    )
    assert r.status_code == 200, r.content
    assert FINANCIAL_KEYS.isdisjoint(r.json())
    assert (r.json()["kind"], r.json()["refund_pct"]) == ("CANCELLATION", None)
    invoice.refresh_from_db()
    assert invoice.lifecycle_status == "CANCELLED"


def test_platform_admin_cancel_and_no_invoice_case(finance_api, fall_enrollment, fee_setup):
    r = finance_api.post(
        f"/api/v1/enrollments/{fall_enrollment.pk}/cancel/",
        {"cancellation_date": "2026-08-20"},
        format="json",
    )
    assert r.status_code == 200
    assert (r.json()["invoice"], r.json()["financial_details_included"]) == (None, True)  # D-W7
    assert r.json()["refund_quote"]["max_refundable"] == "0.00"


@pytest.mark.parametrize("role", ["support_staff", "instructor", "learner"])
@pytest.mark.parametrize(
    ("step", "body"),
    [
        ("withdraw", {"withdrawal_date": "2026-09-17"}),
        ("cancel", {"cancellation_date": "2026-08-20"}),
    ],
)
def test_other_roles_are_refused_and_nothing_changes(
    api_client_for, enr, invoice, role, step, body
):
    before = _snapshot()
    r = api_client_for(role).post(f"/api/v1/enrollments/{enr}/{step}/", body, format="json")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert _snapshot() == before


def test_unauthenticated_is_401(client, enr, invoice):
    r = client.post(
        f"/api/v1/enrollments/{enr}/withdraw/",
        {"withdrawal_date": "2026-09-17"},
        content_type="application/json",
    )
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("body", "details"),
    [
        ({}, {"withdrawal_date": ["This field is required."]}),  # never the clock
        ({"withdrawal_date": "17-09-2026"}, None),
        (
            {"withdrawal_date": "2026-09-17", "refund_pct": "1.00"},
            {"refund_pct": ["Unknown field."]},
        ),
        ({"withdrawal_date": "2026-09-17", "status": "WITHDRAWN"}, {"status": ["Unknown field."]}),
    ],
)
def test_strict_input(finance_api, enr, invoice, body, details):
    before = _snapshot()
    r = finance_api.post(f"/api/v1/enrollments/{enr}/withdraw/", body, format="json")
    assert r.status_code == 400
    if details is not None:
        assert r.json()["error"]["details"] == details
    assert _snapshot() == before


def test_errors_are_enveloped(finance_api, enr, invoice):
    r = finance_api.post(
        f"/api/v1/enrollments/{enr}/withdraw/", {"withdrawal_date": "2026-12-20"}, format="json"
    )
    assert (r.status_code, r.json()["error"]["details"]) == (
        400,
        {"withdrawal_date": ["Must be on or before the term end (2026-12-19)."]},
    )
    finance_api.post(
        f"/api/v1/enrollments/{enr}/withdraw/", {"withdrawal_date": "2026-09-17"}, format="json"
    )
    r = finance_api.post(
        f"/api/v1/enrollments/{enr}/cancel/", {"cancellation_date": "2026-08-20"}, format="json"
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "ILLEGAL_TRANSITION")
    assert r.json()["error"]["details"]["from_status"] == "WITHDRAWN"


# --- D-W6: refunds are requested separately ---


def test_withdrawal_refund_requested_afterwards(finance_api, enr, paid, assert_matches_openapi):
    finance_api.post(
        f"/api/v1/enrollments/{enr}/withdraw/", {"withdrawal_date": "2026-09-17"}, format="json"
    )
    r = finance_api.post(
        "/api/v1/refunds/",
        {
            "payment_id": paid.payment_id,
            "amount": "45000.00",
            "requested_date": "2026-09-18",
            "reason": "WITHDRAWAL",
            "enrollment_id": enr,
        },
        format="json",
    )
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/refunds/", "post")
    body = r.json()
    assert (body["reason"], body["enrollment"], body["payer_type"]) == (
        "WITHDRAWAL",
        enr,
        "GUARDIAN",
    )
    assert body["calculation"]["schedule_pct"] == "0.60"  # the BR-WDR-02 schedule, for audit
    # the existing caps still apply (no extra cap): 45,000.01 would exceed the credit
    r = finance_api.post(
        "/api/v1/refunds/",
        {
            "payment_id": paid.payment_id,
            "amount": "0.01",
            "requested_date": "2026-09-18",
            "reason": "WITHDRAWAL",
            "enrollment_id": enr,
        },
        format="json",
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "REFUND_EXCEEDS_CREDIT_BALANCE")


def test_cancellation_refund_needs_a_cancelled_enrollment_of_the_same_student(
    finance_api, enr, paid, student_with_invoice
):
    def request(reason, enrollment):
        return finance_api.post(
            "/api/v1/refunds/",
            {
                "payment_id": paid.payment_id,
                "amount": "100.00",
                "requested_date": "2026-08-21",
                "reason": reason,
                "enrollment_id": enrollment,
            },
            format="json",
        )

    r = request("CANCELLATION", enr)
    assert r.json()["error"]["details"] == {
        "enrollment_id": [f"{enr} is ENROLLED; a CANCELLATION refund needs a CANCELLED enrollment."]
    }
    _, other = student_with_invoice()
    r = request("CANCELLATION", other.enrollment_id)
    assert r.json()["error"]["details"] == {
        "enrollment_id": [
            f"{other.enrollment_id} belongs to another student than {paid.payment_id}."
        ]
    }
    r = request("OVERPAYMENT", enr)
    assert r.json()["error"]["details"] == {
        "enrollment_id": ["Must be omitted when reason is OVERPAYMENT."]
    }
    finance_api.post(
        f"/api/v1/enrollments/{enr}/cancel/", {"cancellation_date": "2026-08-20"}, format="json"
    )
    r = request("CANCELLATION", enr)
    assert r.status_code == 201, r.content
    assert r.json()["calculation"]["schedule_pct"] is None
