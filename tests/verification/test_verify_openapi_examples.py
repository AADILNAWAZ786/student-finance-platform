"""V-DOC: every documented error example is exactly what the endpoint returns.

The examples are read from the generated OpenAPI document. Each one is replayed against
the live API with a request built to trigger it, and the live ``error`` object must equal
the documented one (code, rule_id, message and details). This catches copy-paste examples,
such as a Student example on a Term endpoint, and examples that drift from behaviour.

A documented error example with no replay recipe fails ``test_every_example_has_a_replay``,
so new endpoints cannot add unchecked examples.
"""

from datetime import date
from decimal import Decimal

import pytest
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.renderers import OpenApiJsonRenderer
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db(databases="__all__")


def _documented_error_examples():
    import json

    doc = json.loads(
        OpenApiJsonRenderer().render(
            SchemaGenerator().get_schema(request=None, public=True), renderer_context={}
        )
    )
    found = []
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            for status, resp in op["responses"].items():
                if not status.startswith(("4", "5")):
                    continue
                for ex in (
                    resp.get("content", {}).get("application/json", {}).get("examples", {}).values()
                ):
                    if "error" in ex["value"]:  # envelope examples (health 503 is a body)
                        found.append(
                            (
                                op["operationId"],
                                int(status),
                                ex["summary"],
                                path,
                                method,
                                ex["value"]["error"],
                            )
                        )
    return found


EXAMPLES = _documented_error_examples()

FEE_V1 = "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
INV = "INV-STU-000001-2026-000001"

# operationId -> (method, path, valid body) used for the generic 401/403 replays
OPERATIONS = {
    "students_list": ("get", "/api/v1/students/", None),
    "students_create": ("post", "/api/v1/students/", {}),
    "students_retrieve": ("get", "/api/v1/students/STU-000001/", None),
    "students_partial_update": ("patch", "/api/v1/students/STU-000001/", {"last_name": "Z"}),
    "students_status_create": ("post", "/api/v1/students/STU-000001/status/", {}),
    "programs_list": ("get", "/api/v1/programs/", None),
    "programs_create": ("post", "/api/v1/programs/", {}),
    "programs_retrieve": ("get", "/api/v1/programs/PRG-001/", None),
    "terms_list": ("get", "/api/v1/terms/", None),
    "terms_create": ("post", "/api/v1/terms/", {}),
    "terms_retrieve": ("get", "/api/v1/terms/TERM-2026-FALL/", None),
    "enrollments_list": ("get", "/api/v1/enrollments/", None),
    "enrollments_create": ("post", "/api/v1/enrollments/", {}),
    "enrollments_retrieve": ("get", "/api/v1/enrollments/ENR-2026-000001/", None),
    "enrollments_events_list": ("get", "/api/v1/enrollments/ENR-2026-000001/events/", None),
    "enrollments_withdraw_create": ("post", "/api/v1/enrollments/ENR-2026-000001/withdraw/", {}),
    "enrollments_cancel_create": ("post", "/api/v1/enrollments/ENR-2026-000001/cancel/", {}),
    # Phase 2: billing
    "fee_schedules_list": ("get", "/api/v1/fee-schedules/", None),
    "fee_schedules_create": ("post", "/api/v1/fee-schedules/", {}),
    "fee_schedules_new_version_create": (
        "post",
        f"/api/v1/fee-schedules/{FEE_V1}/new-version/",
        {},
    ),
    "fee_schedules_history_list": ("get", f"/api/v1/fee-schedules/{FEE_V1}/history/", None),
    "invoices_list": ("get", "/api/v1/invoices/", None),
    "invoices_retrieve": ("get", f"/api/v1/invoices/{INV}/", None),
    "invoices_generate_create": ("post", "/api/v1/invoices/generate/", {}),
    "invoices_void_create": ("post", f"/api/v1/invoices/{INV}/void/", {}),
    "billing_late_fees_assess": ("post", "/api/v1/billing/late-fees/assess/", {}),
    "billing_collection_status_refresh": ("post", "/api/v1/billing/collection-status/refresh/", {}),
    "students_ledger_list": ("get", "/api/v1/students/STU-000001/ledger/", None),
    "students_statement_retrieve": ("get", "/api/v1/students/STU-000001/statement/", None),
    "students_balance_retrieve": ("get", "/api/v1/students/STU-000001/balance/", None),
    # Phase 3: scholarships
    "scholarship_types_list": ("get", "/api/v1/scholarship-types/", None),
    "scholarship_types_create": ("post", "/api/v1/scholarship-types/", {}),
    "scholarships_eligibility_checks_create": (
        "post",
        "/api/v1/scholarships/eligibility-checks/",
        {},
    ),
    "eligibility_list": ("get", "/api/v1/eligibility/", None),
    "scholarships_create": ("post", "/api/v1/scholarships/", {}),
    "scholarships_list": ("get", "/api/v1/scholarships/", None),
    "scholarships_retrieve": ("get", "/api/v1/scholarships/SCH-2026-000001/", None),
    "scholarships_apply_create": ("post", "/api/v1/scholarships/SCH-2026-000001/apply/", {}),
    # Phase 4: payments and refunds
    "payments_list": ("get", "/api/v1/payments/", None),
    "payments_create": ("post", "/api/v1/payments/", {}),
    "payments_retrieve": ("get", "/api/v1/payments/PAY-2026-000001/", None),
    "payments_allocate_create": ("post", "/api/v1/payments/PAY-2026-000001/allocate/", {}),
    "payments_allocations_list": ("get", "/api/v1/payments/PAY-2026-000001/allocations/", None),
    "payment_allocations_list": ("get", "/api/v1/payment-allocations/", None),
    "refunds_list": ("get", "/api/v1/refunds/", None),
    "refunds_create": ("post", "/api/v1/refunds/", {}),
    "refunds_retrieve": ("get", "/api/v1/refunds/REF-2026-000001/", None),
    "refunds_approve_create": ("post", "/api/v1/refunds/REF-2026-000001/approve/", {}),
    "refunds_process_create": ("post", "/api/v1/refunds/REF-2026-000001/process/", {}),
    "refunds_reject_create": ("post", "/api/v1/refunds/REF-2026-000001/reject/", {}),
    "students_refundable_retrieve": ("get", "/api/v1/students/STU-000001/refundable/", None),
    # Phase 6: reconciliation
    "reconciliation_runs_list": ("get", "/api/v1/reconciliation/runs/", None),
    "reconciliation_runs_create": ("post", "/api/v1/reconciliation/runs/", {}),
    "reconciliation_runs_retrieve": ("get", "/api/v1/reconciliation/runs/RECON-2026-000001/", None),
    "anomalies_list": ("get", "/api/v1/anomalies/", None),
    "anomalies_retrieve": ("get", "/api/v1/anomalies/FND-2026-000001/", None),
    "balance_snapshots_list": ("get", "/api/v1/balance-snapshots/", None),
    "balance_snapshots_create": ("post", "/api/v1/balance-snapshots/", {}),
    "balance_snapshots_retrieve": ("get", "/api/v1/balance-snapshots/SNAP-2026-000001/", None),
    # Phase 7: data generation and evaluation (platform_admin only)
    "datagen_runs_list": ("get", "/api/v1/datagen/runs/", None),
    "datagen_runs_create": ("post", "/api/v1/datagen/runs/", {}),
    "datagen_runs_retrieve": ("get", "/api/v1/datagen/runs/GEN-2026-0001/", None),
    "datagen_runs_evaluation_retrieve": (
        "get",
        "/api/v1/datagen/runs/GEN-2026-0001/evaluation/",
        None,
    ),
}

TERM_2027 = {
    "year": 2027,
    "season": "SPRING",
    "is_billable": False,
    "invoice_date": "2027-01-04",
    "due_date": "2027-02-01",
    "start_date": "2027-01-25",
    "census_date": "2027-02-23",  # should be 02-22
    "end_date": "2027-05-08",
}


def _enroll(**kw):
    return {
        "student": "STU-000001",
        "term": "TERM-2026-FALL",
        "credits": 16,
        "enrolled_on": "2026-08-20",
        **kw,
    }


def _graduate_then_reactivate(w):
    from students import services

    services.change_status(
        w.student, to_status="GRADUATED", event_date=w.student_date, reason="DONE", actor="t"
    )
    return w.admin.post(
        "/api/v1/students/STU-000001/status/", {"to_status": "ACTIVE", "reason": "X"}, format="json"
    )


BILLING_OPERATIONS = {
    op
    for op in OPERATIONS
    if op.startswith(
        ("fee_", "invoices_", "billing_", "scholarship", "eligibility", "payment", "refund")
    )
} | {
    "students_ledger_list",
    "students_statement_retrieve",
    "students_balance_retrieve",
    "students_refundable_retrieve",
}

TUITION = {
    "program": "PRG-001",
    "term": "TERM-2026-FALL",
    "fee_type": "TUITION",
    "residency": "DOMESTIC",
    "charge_basis": "PER_CREDIT",
    "amount": "9000.00",
    "effective_from": "2026-01-01",
}


def _new_version_of_v1_twice(w):
    from billing import services
    from billing.models import FeeSchedule

    services.new_fee_schedule_version(
        FeeSchedule.objects.get(pk=FEE_V1),
        amount=Decimal("9360.00"),
        effective_from=date(2026, 8, 1),
    )
    return w.finance.post(
        f"/api/v1/fee-schedules/{FEE_V1}/new-version/",
        {"amount": "9500.00", "effective_from": "2026-09-01"},
        format="json",
    )


def _generate_with_tuition_waiver(w):
    from academics import services as academics
    from students import services as students

    other = students.create_student(
        first_name="Ravi",
        last_name="Iyer",
        email="ravi@example.edu",
        date_of_birth=date(2006, 5, 1),
        residency="DOMESTIC",
        program=w.student.program,
        admit_term=w.student.admit_term,
        gpa=Decimal("3.10"),
        need_index=Decimal("0.20"),
        event_date=date(2026, 1, 5),
        actor="t",
    )
    enrollment = academics.enroll(
        student=other, term=w.fall, credits=16, enrolled_on=date(2026, 8, 20), actor="t"
    )
    return w.finance.post(
        "/api/v1/invoices/generate/",
        {
            "enrollment_id": enrollment.enrollment_id,
            "waivers": [{"fee_type": "TUITION", "amount": "1.00", "reason": "HARDSHIP"}],
        },
        format="json",
    )


def _generate_for_summer(w):
    from academics import services as academics

    enrollment = academics.enroll(
        student=w.student, term=w.summer, credits=16, enrolled_on=date(2026, 5, 20), actor="t"
    )
    assert enrollment.enrollment_id == "ENR-2026-000003"
    return w.finance.post(
        "/api/v1/invoices/generate/", {"enrollment_id": enrollment.enrollment_id}, format="json"
    )


def _void_twice(w):
    from billing import services

    services.void_invoice(w.invoice, void_date=date(2026, 8, 21), actor="t")
    return w.finance.post(
        f"/api/v1/invoices/{INV}/void/", {"void_date": "2026-08-22"}, format="json"
    )


def _void_with_scholarship(w):
    """INVOICE_HAS_ALLOCATIONS with a real applied scholarship (Phase 3): a 40,000 merit
    award on STU-000001's invoice, applied, then the void request."""
    from scholarships import services

    services.apply_scholarship(_award_merit(w, requested_amount=Decimal("40000.00")), actor="t")
    return w.finance.post(
        f"/api/v1/invoices/{INV}/void/", {"void_date": "2026-08-21"}, format="json"
    )


FULL_RIDE_TYPE = {
    "name": "Second Full-Ride",
    "category": "FULL_RIDE",
    "exclusive_group": "FULL_RIDE",
    "residency": "ALL",
    "first_term_only": False,
    "program_restricted": False,
    "coverage_scope": "ALL_FEE_LINES",
    "award_basis": "PERCENT",
    "award_value": "100.00",
    "max_amount_per_term": "600000.00",
    "priority": 0,
    "effective_from": "2026-01-01",
}
RESTRICTED_TYPE = {
    **FULL_RIDE_TYPE,
    "name": "Restricted",
    "category": "PROGRAM",
    "exclusive_group": None,
    "coverage_scope": "ELIGIBLE_LINES_ONLY",
    "priority": 9,
    "program_restricted": True,
}


def _award_merit(w, **kw):
    from scholarships import services
    from scholarships.models import Eligibility

    return services.award_scholarship(
        eligibility=Eligibility.objects.get(pk="ELG-2026-000002"),
        invoice=w.invoice,
        actor="t",
        **kw,
    )


def _second_merit_award(w):
    from scholarships import services

    _award_merit(w)
    services.evaluate_eligibility(student=w.student, term=w.fall)  # ELG-2026-000008..14
    return w.finance.post(
        "/api/v1/scholarships/",
        {"eligibility_id": "ELG-2026-000009", "invoice_id": INV},
        format="json",
    )


def _apply_before_award_date(w):
    _award_merit(w)
    return w.finance.post(
        "/api/v1/scholarships/SCH-2026-000001/apply/", {"allocated_on": "2026-08-19"}, format="json"
    )


def _apply_twice(w):
    from scholarships import services

    services.apply_scholarship(_award_merit(w), actor="t")
    return w.finance.post("/api/v1/scholarships/SCH-2026-000001/apply/", {}, format="json")


def _apply_above_base(w):
    """BR-SCH-04 at apply: an award inserted without the award-stage guardrail (as an
    injected anomaly would be) cannot be applied above the eligible fee base."""
    from scholarships.models import Eligibility, Scholarship

    elig = Eligibility.objects.get(pk="ELG-2026-000002")
    Scholarship.objects.create(
        scholarship_id="SCH-2026-000001",
        student=w.student,
        scholarship_type=elig.scholarship_type,
        term=w.fall,
        eligibility=elig,
        invoice=w.invoice,
        awarded_amount=Decimal("200000.00"),
        award_date=date(2026, 8, 20),
        status="OFFERED",
    )
    return w.finance.post("/api/v1/scholarships/SCH-2026-000001/apply/", {}, format="json")


SPECIFIC = {
    ("students_list", 400, "Invalid status filter"): lambda w: w.admin.get(
        "/api/v1/students/", {"status": "SUSPENDED"}
    ),
    # a complete, otherwise valid body: only gpa is wrong (a JSON number, not a string)
    ("students_create", 400, "gpa sent as a JSON number"): lambda w: w.admin.post(
        "/api/v1/students/",
        {
            "first_name": "Asha",
            "last_name": "Rao",
            "email": "new@example.edu",
            "date_of_birth": "2006-03-14",
            "residency": "DOMESTIC",
            "program": "PRG-001",
            "admit_term": "TERM-2026-SPRING",
            "gpa": 3.7,
            "need_index": "0.35",
        },
        format="json",
    ),
    ("students_retrieve", 404, "Unknown Student"): lambda w: w.admin.get(
        "/api/v1/students/STU-999999/"
    ),
    ("students_partial_update", 400, "Non-contact field"): lambda w: w.admin.patch(
        "/api/v1/students/STU-000001/", {"gpa": "4.00"}, format="json"
    ),
    ("students_partial_update", 404, "Unknown Student"): lambda w: w.admin.patch(
        "/api/v1/students/STU-999999/", {"last_name": "Z"}, format="json"
    ),
    ("students_status_create", 400, "Unknown status"): lambda w: w.admin.post(
        "/api/v1/students/STU-000001/status/",
        {"to_status": "SUSPENDED", "reason": "X"},
        format="json",
    ),
    ("students_status_create", 404, "Unknown Student"): lambda w: w.admin.post(
        "/api/v1/students/STU-999999/status/",
        {"to_status": "ON_HOLD", "reason": "X"},
        format="json",
    ),
    ("students_status_create", 409, "Illegal transition"): _graduate_then_reactivate,
    ("programs_list", 400, "Invalid level filter"): lambda w: w.admin.get(
        "/api/v1/programs/", {"level": "PHD"}
    ),
    ("programs_create", 400, "Credit range out of bounds"): lambda w: w.admin.post(
        "/api/v1/programs/",
        {
            "code": "X",
            "name": "X",
            "level": "UG",
            "department": "X",
            "is_stem": False,
            "has_lab": False,
            "min_credits": 20,
            "max_credits": 12,
        },
        format="json",
    ),
    ("programs_retrieve", 404, "Unknown Program"): lambda w: w.admin.get(
        "/api/v1/programs/PRG-999/"
    ),
    ("terms_list", 400, "Invalid season filter"): lambda w: w.admin.get(
        "/api/v1/terms/", {"season": "WINTER"}
    ),
    ("terms_create", 400, "Census date not start + 28"): lambda w: w.admin.post(
        "/api/v1/terms/", TERM_2027, format="json"
    ),
    ("terms_retrieve", 404, "Unknown Term"): lambda w: w.admin.get("/api/v1/terms/TERM-2030-FALL/"),
    ("enrollments_list", 400, "Invalid status filter"): lambda w: w.admin.get(
        "/api/v1/enrollments/", {"status": "PAUSED"}
    ),
    ("enrollments_create", 400, "After the add/drop window"): lambda w: w.admin.post(
        "/api/v1/enrollments/", _enroll(enrolled_on="2026-09-15"), format="json"
    ),
    ("enrollments_create", 409, "Already enrolled"): lambda w: w.admin.post(
        "/api/v1/enrollments/", _enroll(), format="json"
    ),
    ("enrollments_create", 422, "Credits outside the program range"): lambda w: w.admin.post(
        "/api/v1/enrollments/", _enroll(credits=21), format="json"
    ),
    ("enrollments_create", 422, "Term not billable"): lambda w: w.admin.post(
        "/api/v1/enrollments/",
        _enroll(term="TERM-2025-FALL", enrolled_on="2025-09-01"),
        format="json",
    ),
    ("enrollments_retrieve", 404, "Unknown Enrollment"): lambda w: w.admin.get(
        "/api/v1/enrollments/ENR-2026-999999/"
    ),
    ("enrollments_events_list", 404, "Unknown Enrollment"): lambda w: w.admin.get(
        "/api/v1/enrollments/ENR-2026-999999/events/"
    ),
    ("health_check", 405, "POST not allowed"): lambda w: APIClient().post(
        "/api/v1/health/", {}, format="json"
    ),
    # --- Phase 2: billing (w.finance = platform_admin) ---
    ("fee_schedules_list", 400, "Invalid fee_type filter"): lambda w: w.finance.get(
        "/api/v1/fee-schedules/", {"fee_type": "PARKING"}
    ),
    ("fee_schedules_create", 400, "Residency not allowed for the fee type"): lambda w: (
        w.finance.post("/api/v1/fee-schedules/", {**TUITION, "residency": "ALL"}, format="json")
    ),
    ("fee_schedules_create", 400, "Server-controlled field sent"): lambda w: w.finance.post(
        "/api/v1/fee-schedules/", {**TUITION, "allocation_priority": 5}, format="json"
    ),
    ("fee_schedules_create", 422, "Version 1 already exists"): lambda w: w.finance.post(
        "/api/v1/fee-schedules/", TUITION, format="json"
    ),
    ("fee_schedules_new_version_create", 400, "Amount sent as a JSON number"): lambda w: (
        w.finance.post(
            f"/api/v1/fee-schedules/{FEE_V1}/new-version/",
            {"amount": 9360, "effective_from": "2026-08-01"},
            format="json",
        )
    ),
    ("fee_schedules_new_version_create", 404, "Unknown FeeSchedule"): lambda w: w.finance.post(
        "/api/v1/fee-schedules/FEE-NOPE/new-version/",
        {"amount": "1.00", "effective_from": "2026-08-01"},
        format="json",
    ),
    ("fee_schedules_new_version_create", 422, "effective_from not after the predecessor"): (
        lambda w: w.finance.post(
            f"/api/v1/fee-schedules/{FEE_V1}/new-version/",
            {"amount": "9360.00", "effective_from": "2026-01-01"},
            format="json",
        )
    ),
    ("fee_schedules_new_version_create", 422, "Not the latest version"): _new_version_of_v1_twice,
    ("fee_schedules_history_list", 404, "Unknown FeeSchedule"): lambda w: w.finance.get(
        "/api/v1/fee-schedules/FEE-NOPE/history/"
    ),
    ("invoices_list", 400, "Invalid collection_status filter"): lambda w: w.finance.get(
        "/api/v1/invoices/", {"collection_status": "LATE"}
    ),
    ("invoices_retrieve", 404, "Unknown Invoice"): lambda w: w.finance.get(
        "/api/v1/invoices/INV-NOPE/"
    ),
    (
        "invoices_generate_create",
        400,
        "Waiver on a non-waivable fee",
    ): _generate_with_tuition_waiver,
    ("invoices_generate_create", 400, "Unknown enrollment"): lambda w: w.finance.post(
        "/api/v1/invoices/generate/", {"enrollment_id": "ENR-2026-999999"}, format="json"
    ),
    ("invoices_generate_create", 409, "Invoice already generated"): lambda w: w.finance.post(
        "/api/v1/invoices/generate/", {"enrollment_id": "ENR-2026-000001"}, format="json"
    ),
    ("invoices_generate_create", 409, "Enrollment not ENROLLED"): lambda w: w.finance.post(
        "/api/v1/invoices/generate/", {"enrollment_id": "ENR-2026-000002"}, format="json"
    ),
    ("invoices_generate_create", 422, "No fee schedule in force"): _generate_for_summer,
    ("invoices_void_create", 400, "void_date before the issue date"): lambda w: w.finance.post(
        f"/api/v1/invoices/{INV}/void/", {"void_date": "2026-08-19"}, format="json"
    ),
    ("invoices_void_create", 400, "void_date missing"): lambda w: w.finance.post(
        f"/api/v1/invoices/{INV}/void/", {}, format="json"
    ),
    ("invoices_void_create", 404, "Unknown Invoice"): lambda w: w.finance.post(
        "/api/v1/invoices/INV-NOPE/void/", {"void_date": "2026-08-21"}, format="json"
    ),
    ("invoices_void_create", 409, "Already void"): _void_twice,
    ("invoices_void_create", 409, "Scholarship or payment applied"): _void_with_scholarship,
    ("billing_late_fees_assess", 400, "as_of_date missing"): lambda w: w.finance.post(
        "/api/v1/billing/late-fees/assess/", {}, format="json"
    ),
    ("billing_collection_status_refresh", 400, "as_of_date outside 2026"): lambda w: w.finance.post(
        "/api/v1/billing/collection-status/refresh/", {"as_of_date": "2027-01-01"}, format="json"
    ),
    ("students_ledger_list", 404, "Unknown Student"): lambda w: w.finance.get(
        "/api/v1/students/STU-999999/ledger/"
    ),
    ("students_statement_retrieve", 404, "Unknown Student"): lambda w: w.finance.get(
        "/api/v1/students/STU-999999/statement/"
    ),
    ("students_balance_retrieve", 400, "as_of not a date"): lambda w: w.finance.get(
        "/api/v1/students/STU-000001/balance/", {"as_of": "16-08-2026"}
    ),
    ("students_balance_retrieve", 404, "Unknown Student"): lambda w: w.finance.get(
        "/api/v1/students/STU-999999/balance/"
    ),
    # --- Phase 3: scholarships ---
    ("scholarship_types_list", 400, "Invalid category filter"): lambda w: w.finance.get(
        "/api/v1/scholarship-types/", {"category": "SPORTS"}
    ),
    ("scholarship_types_create", 400, "Program list missing for a restricted type"): lambda w: (
        w.finance.post("/api/v1/scholarship-types/", RESTRICTED_TYPE, format="json")
    ),
    ("scholarship_types_create", 400, "Second ALL_FEE_LINES type"): lambda w: w.finance.post(
        "/api/v1/scholarship-types/", FULL_RIDE_TYPE, format="json"
    ),
    ("scholarships_eligibility_checks_create", 400, "Unknown student"): lambda w: w.finance.post(
        "/api/v1/scholarships/eligibility-checks/",
        {"student_id": "STU-999999", "term_id": "TERM-2026-FALL"},
        format="json",
    ),
    ("scholarships_eligibility_checks_create", 400, "evaluated_on sent"): lambda w: w.finance.post(
        "/api/v1/scholarships/eligibility-checks/",
        {"student_id": "STU-000001", "term_id": "TERM-2026-FALL", "evaluated_on": "2026-09-01"},
        format="json",
    ),
    ("scholarships_eligibility_checks_create", 422, "Term not billable"): lambda w: w.finance.post(
        "/api/v1/scholarships/eligibility-checks/",
        {"student_id": "STU-000001", "term_id": "TERM-2025-SPRING"},
        format="json",
    ),
    ("eligibility_list", 400, "Invalid evaluated_on filter"): lambda w: w.finance.get(
        "/api/v1/eligibility/", {"evaluated_on": "2026-13-01"}
    ),
    ("scholarships_create", 400, "requested_amount not positive"): lambda w: w.finance.post(
        "/api/v1/scholarships/",
        {"eligibility_id": "ELG-2026-000002", "invoice_id": INV, "requested_amount": "0.00"},
        format="json",
    ),
    ("scholarships_create", 409, "Exclusive group conflict"): _second_merit_award,
    ("scholarships_create", 422, "Not a positive eligibility"): lambda w: w.finance.post(
        "/api/v1/scholarships/",
        {"eligibility_id": "ELG-2026-000003", "invoice_id": INV},
        format="json",
    ),
    ("scholarships_create", 422, "Over the maximum award"): lambda w: w.finance.post(
        "/api/v1/scholarships/",
        {"eligibility_id": "ELG-2026-000002", "invoice_id": INV, "requested_amount": "75000.01"},
        format="json",
    ),
    ("scholarships_list", 400, "Invalid status filter"): lambda w: w.finance.get(
        "/api/v1/scholarships/", {"status": "PENDING"}
    ),
    ("scholarships_retrieve", 404, "Unknown Scholarship"): lambda w: w.finance.get(
        "/api/v1/scholarships/SCH-2026-999999/"
    ),
    ("scholarships_apply_create", 400, "allocated_on before the award"): _apply_before_award_date,
    ("scholarships_apply_create", 404, "Unknown Scholarship"): lambda w: w.finance.post(
        "/api/v1/scholarships/SCH-2026-999999/apply/", {}, format="json"
    ),
    ("scholarships_apply_create", 409, "Already applied"): _apply_twice,
    ("scholarships_apply_create", 422, "Above the eligible fee base"): _apply_above_base,
}


# --- Phase 4 recipes: payments and refunds ---------------------------------------------------
PAY1 = "PAY-2026-000001"
REF1 = "REF-2026-000001"


def _pay(w, amount, *, ref="UTR-0001", on=date(2026, 8, 20), auto=False, student=None):
    from payments import services

    payment, _ = services.record_payment(
        student=student or w.student,
        amount=Decimal(amount),
        received_date=on,
        method="UPI",
        payer_type="GUARDIAN",
        external_reference=ref,
        auto_allocate=auto,
        actor="t",
    )
    return payment


def _manual(w, payment, *pairs, on=date(2026, 8, 20)):
    from billing.models import InvoiceLine
    from payments import services

    lines = [(InvoiceLine.objects.get(pk=f"{INV}-{n}"), Decimal(a)) for n, a in pairs]
    return services.allocate_payment(
        payment, mode="MANUAL", lines=lines, allocated_on=on, actor="t"
    )


def _refund(payment, amount, on=date(2026, 8, 21)):
    from payments import services

    return services.request_refund(
        payment=payment, amount=Decimal(amount), requested_date=on, actor="t"
    )


def _second_payment_with_same_reference(w):
    _pay(w, "1000.00")
    return w.finance.post(
        "/api/v1/payments/",
        {
            "student_id": "STU-000001",
            "amount": "1000.00",
            "received_date": "2026-08-20",
            "method": "UPI",
            "payer_type": "GUARDIAN",
            "external_reference": "UTR-0001",
        },
        format="json",
    )


def _allocate_at_the_99_row_limit(w):
    """49 × (allocate 1.00 + release it) + 1 allocation = 99 rows; one more cannot fit."""
    from payments import services

    payment = _pay(w, "50000.00")
    for _ in range(49):
        (row,) = _manual(w, payment, ("L01", "1.00")).allocations
        services.deallocate(
            row,
            amount=Decimal("1.00"),
            rule_applied="SCHOLARSHIP_PRECEDENCE",
            on_date=date(2026, 8, 20),
            actor="t",
        )
    _manual(w, payment, ("L01", "1.00"))
    return w.finance.post(f"/api/v1/payments/{PAY1}/allocate/", {"mode": "AUTO"}, format="json")


def _manual_allocation(w, line, amount):
    return w.finance.post(
        f"/api/v1/payments/{PAY1}/allocate/",
        {
            "mode": "MANUAL",
            "allocated_on": "2026-08-20",
            "lines": [{"invoice_line_id": line, "amount": amount}],
        },
        format="json",
    )


def _allocate_to_another_students_line(w):
    from academics import services as academics
    from billing import services as billing
    from students import services as students

    _pay(w, "50000.00")
    other = students.create_student(
        first_name="Meera",
        last_name="Nair",
        email="meera@example.edu",
        date_of_birth=date(2006, 7, 1),
        residency="DOMESTIC",
        program=w.student.program,
        admit_term=w.student.admit_term,
        gpa=Decimal("3.20"),
        need_index=Decimal("0.30"),
        event_date=date(2026, 1, 5),
        actor="t",
    )
    enrollment = academics.enroll(
        student=other, term=w.fall, credits=16, enrolled_on=date(2026, 8, 20), actor="t"
    )
    billing.generate_invoice(enrollment=enrollment, actor="t")
    return _manual_allocation(w, "INV-STU-000002-2026-000001-L01", "100.00")


def _adjust_tuition(w, amount, on):
    """An ADJUSTMENT line on tuition, as a Phase 5 withdrawal credit would post it."""
    from billing import services as billing
    from billing.models import InvoiceLine

    tuition = InvoiceLine.objects.get(pk=f"{INV}-L01")
    line = InvoiceLine.objects.create(
        line_id=f"{INV}-L20",
        invoice=w.invoice,
        line_type="ADJUSTMENT",
        fee_type="TUITION",
        description="credit",
        quantity=1,
        unit_amount=Decimal(amount),
        gross_amount=Decimal(amount),
        net_amount=Decimal(amount),
        adjusts_line=tuition,
        adjustment_reason="WITHDRAWAL",
        is_scholarship_eligible=False,
        is_refundable=False,
        is_waivable=False,
        allocation_priority=10,
        posted_date=on,
    )
    billing.post_ledger(line)


def _refund_post(w, amount, payment=PAY1):
    return w.finance.post(
        "/api/v1/refunds/",
        {"payment_id": payment, "amount": amount, "requested_date": "2026-09-18"},
        format="json",
    )


def _refund_above_net_paid(w):
    """Net paid binds only when the credit balance exceeds it: here a tuition credit posted
    before the scholarship it covers is reversed (the Phase 5 release would follow)."""
    from scholarships import services

    services.apply_scholarship(_award_merit(w), actor="t")  # 75,000 on tuition
    _pay(w, "89000.00")  # unallocated
    _adjust_tuition(w, "-140000.00", date(2026, 9, 10))
    return _refund_post(w, "89000.01")


def _refund_above_credit_available(w):
    payment = _pay(w, "174000.00")  # ledger −10,000
    _manual(w, payment, ("L01", "144000.00"), ("L02", "6000.00"))  # 24,000 left
    _refund(payment, "5000.00")  # reserves 5,000 of the 10,000 credit
    return _refund_post(w, "6000.00")


def _refund_above_payment_available(w):
    _pay(w, "194000.00", auto=True)  # 164,000 allocated: 30,000 available
    _pay(w, "10000.00", ref="UTR-0002")  # more credit, on another payment
    return _refund_post(w, "30000.01")


def _late_fee_after_refund_request(w, *, approve_first):
    """A 10,000 refund of the whole credit, then a 500 late fee (2% of the 14,000 still owed,
    raised to the minimum) uses part of that credit before the next step."""
    from billing import services as billing
    from payments import services

    payment = _pay(w, "174000.00")
    _manual(w, payment, ("L01", "144000.00"), ("L02", "6000.00"))  # 14,000 owed, 24,000 free
    refund = _refund(payment, "10000.00")
    if approve_first:
        services.approve_refund(refund, on_date=date(2026, 8, 22), actor="t")
    billing.assess_late_fees(as_of=date(2026, 9, 29), dry_run=False, actor="t")
    step = "process" if approve_first else "approve"
    body = {"processed_date": "2026-09-30"} if approve_first else {"on_date": "2026-09-30"}
    return w.finance.post(f"/api/v1/refunds/{REF1}/{step}/", body, format="json")


def _requested_refund(w):
    payment = _pay(w, "174000.00")
    return _refund(payment, "1000.00")


def _approve_twice(w):
    from payments import services

    services.approve_refund(_requested_refund(w), on_date=date(2026, 8, 22), actor="t")
    return w.finance.post(
        f"/api/v1/refunds/{REF1}/approve/", {"on_date": "2026-08-23"}, format="json"
    )


def _process_before_approval(w):
    _requested_refund(w)
    return w.finance.post(
        f"/api/v1/refunds/{REF1}/process/", {"processed_date": "2026-08-23"}, format="json"
    )


def _reject_processed(w):
    from payments import services

    refund = services.approve_refund(_requested_refund(w), on_date=date(2026, 8, 22), actor="t")
    services.process_refund(refund, processed_date=date(2026, 8, 23), actor="t")
    return w.finance.post(
        f"/api/v1/refunds/{REF1}/reject/", {"on_date": "2026-08-24"}, format="json"
    )


PHASE4_SPECIFIC = {
    ("payments_list", 400, "Invalid payer_type filter"): lambda w: w.finance.get(
        "/api/v1/payments/", {"payer_type": "PARENT"}
    ),
    ("payments_create", 400, "Amount not positive"): lambda w: w.finance.post(
        "/api/v1/payments/",
        {
            "student_id": "STU-000001",
            "amount": "0.00",
            "received_date": "2026-08-20",
            "method": "UPI",
            "payer_type": "SELF",
            "external_reference": "UTR-ZERO",
        },
        format="json",
    ),
    ("payments_create", 400, "Received before the first invoice"): lambda w: w.finance.post(
        "/api/v1/payments/",
        {
            "student_id": "STU-000001",
            "amount": "1000.00",
            "received_date": "2026-08-19",
            "method": "UPI",
            "payer_type": "SELF",
            "external_reference": "UTR-EARLY",
        },
        format="json",
    ),
    ("payments_create", 409, "Duplicate external_reference"): _second_payment_with_same_reference,
    ("payments_retrieve", 404, "Unknown Payment"): lambda w: w.finance.get(
        "/api/v1/payments/PAY-2026-999999/"
    ),
    ("payments_allocate_create", 400, "MANUAL without lines"): lambda w: (
        _pay(w, "1000.00"),
        w.finance.post(
            f"/api/v1/payments/{PAY1}/allocate/",
            {"mode": "MANUAL", "allocated_on": "2026-08-20"},
            format="json",
        ),
    )[1],
    ("payments_allocate_create", 404, "Unknown Payment"): lambda w: w.finance.post(
        "/api/v1/payments/PAY-2026-999999/allocate/", {"mode": "AUTO"}, format="json"
    ),
    ("payments_allocate_create", 409, "Allocation ID limit reached"): _allocate_at_the_99_row_limit,
    ("payments_allocate_create", 422, "More than the payment's available amount"): lambda w: (
        _pay(w, "50000.00"),
        _manual_allocation(w, f"{INV}-L01", "50000.01"),
    )[1],
    ("payments_allocate_create", 422, "More than the line's open amount"): lambda w: (
        _pay(w, "50000.00"),
        _manual_allocation(w, f"{INV}-L02", "6000.01"),
    )[1],
    ("payments_allocate_create", 422, "Another student's line"): _allocate_to_another_students_line,
    ("payments_allocations_list", 404, "Page past the last page"): lambda w: (
        _pay(w, "1000.00"),
        w.finance.get(f"/api/v1/payments/{PAY1}/allocations/", {"page": 99}),
    )[1],
    ("payments_allocations_list", 404, "Unknown Payment"): lambda w: w.finance.get(
        "/api/v1/payments/PAY-2026-999999/allocations/"
    ),
    ("payment_allocations_list", 400, "Invalid allocation_type filter"): lambda w: w.finance.get(
        "/api/v1/payment-allocations/", {"allocation_type": "REVERSE"}
    ),
    ("refunds_list", 400, "Invalid status filter"): lambda w: w.finance.get(
        "/api/v1/refunds/", {"status": "PAID"}
    ),
    ("refunds_retrieve", 404, "Unknown Refund"): lambda w: w.finance.get(
        "/api/v1/refunds/REF-2026-999999/"
    ),
    ("refunds_create", 400, "WITHDRAWAL without enrollment_id"): lambda w: (
        _pay(w, "174000.00"),
        w.finance.post(
            "/api/v1/refunds/",
            {
                "payment_id": PAY1,
                "amount": "100.00",
                "requested_date": "2026-09-18",
                "reason": "WITHDRAWAL",
            },
            format="json",
        ),
    )[1],
    ("refunds_create", 400, "WITHDRAWAL for an enrollment still ENROLLED"): lambda w: (
        _pay(w, "174000.00"),
        w.finance.post(
            "/api/v1/refunds/",
            {
                "payment_id": PAY1,
                "amount": "100.00",
                "requested_date": "2026-09-18",
                "reason": "WITHDRAWAL",
                "enrollment_id": "ENR-2026-000001",
            },
            format="json",
        ),
    )[1],
    ("refunds_create", 422, "More than net paid"): _refund_above_net_paid,
    ("refunds_create", 422, "More than the credit available"): _refund_above_credit_available,
    ("refunds_create", 422, "More than the payment's available amount"): (
        _refund_above_payment_available
    ),
    ("refunds_approve_create", 400, "on_date before the request"): lambda w: (
        _requested_refund(w),
        w.finance.post(
            f"/api/v1/refunds/{REF1}/approve/", {"on_date": "2026-08-20"}, format="json"
        ),
    )[1],
    ("refunds_approve_create", 404, "Unknown Refund"): lambda w: w.finance.post(
        "/api/v1/refunds/REF-2026-999999/approve/", {"on_date": "2026-08-22"}, format="json"
    ),
    ("refunds_approve_create", 409, "Already approved"): _approve_twice,
    ("refunds_approve_create", 422, "Credit used up since the request (late fee)"): lambda w: (
        _late_fee_after_refund_request(w, approve_first=False)
    ),
    ("refunds_process_create", 400, "processed_date missing"): lambda w: (
        _requested_refund(w),
        w.finance.post(f"/api/v1/refunds/{REF1}/process/", {}, format="json"),
    )[1],
    ("refunds_process_create", 404, "Unknown Refund"): lambda w: w.finance.post(
        "/api/v1/refunds/REF-2026-999999/process/",
        {"processed_date": "2026-08-23"},
        format="json",
    ),
    ("refunds_process_create", 409, "Not approved yet"): _process_before_approval,
    ("refunds_process_create", 422, "Credit used up since approval (late fee)"): lambda w: (
        _late_fee_after_refund_request(w, approve_first=True)
    ),
    ("refunds_reject_create", 400, "on_date missing"): lambda w: (
        _requested_refund(w),
        w.finance.post(f"/api/v1/refunds/{REF1}/reject/", {}, format="json"),
    )[1],
    ("refunds_reject_create", 404, "Unknown Refund"): lambda w: w.finance.post(
        "/api/v1/refunds/REF-2026-999999/reject/", {"on_date": "2026-08-22"}, format="json"
    ),
    ("refunds_reject_create", 409, "Already processed"): _reject_processed,
    ("students_refundable_retrieve", 404, "Unknown Student"): lambda w: w.finance.get(
        "/api/v1/students/STU-999999/refundable/"
    ),
}
SPECIFIC.update(PHASE4_SPECIFIC)


# --- Phase 5 recipes: withdrawals and cancellations ---------------------------------------------
ENR_FALL, ENR_DONE = "ENR-2026-000001", "ENR-2026-000002"  # FALL (ENROLLED), SPRING (COMPLETED)


def _exit(w, enrollment, step, body):
    return w.admin.post(f"/api/v1/enrollments/{enrollment}/{step}/", body, format="json")


def _withdraw_with_a_payment_at_98_rows(w):
    """48 × (allocate 1.00 + release it) = 96 rows, then 40,000 on L01 and 6,000 on L02 = 98.
    A withdrawal on day 0 (80%) must release on both lines: 2 more rows do not fit."""
    from payments import services

    payment = _pay(w, "50000.00")
    for _ in range(48):
        (row,) = _manual(w, payment, ("L01", "1.00")).allocations
        services.deallocate(
            row,
            amount=Decimal("1.00"),
            rule_applied="SCHOLARSHIP_PRECEDENCE",
            on_date=date(2026, 8, 20),
            actor="t",
        )
    _manual(w, payment, ("L01", "40000.00"), ("L02", "6000.00"))
    return _exit(w, ENR_FALL, "withdraw", {"withdrawal_date": "2026-09-07"})


PHASE5_SPECIFIC = {
    ("enrollments_withdraw_create", 400, "Before the term start"): lambda w: _exit(
        w, ENR_FALL, "withdraw", {"withdrawal_date": "2026-09-06"}
    ),
    ("enrollments_withdraw_create", 404, "Unknown Enrollment"): lambda w: _exit(
        w, "ENR-2026-999999", "withdraw", {"withdrawal_date": "2026-09-17"}
    ),
    ("enrollments_withdraw_create", 409, "Not ENROLLED"): lambda w: _exit(
        w, ENR_DONE, "withdraw", {"withdrawal_date": "2026-09-17"}
    ),
    (
        "enrollments_withdraw_create",
        409,
        "A release needs allocation IDs a payment no longer has",
    ): _withdraw_with_a_payment_at_98_rows,
    ("enrollments_cancel_create", 400, "On or after the term start"): lambda w: _exit(
        w, ENR_FALL, "cancel", {"cancellation_date": "2026-09-07"}
    ),
    ("enrollments_cancel_create", 404, "Unknown Enrollment"): lambda w: _exit(
        w, "ENR-2026-999999", "cancel", {"cancellation_date": "2026-08-25"}
    ),
    ("enrollments_cancel_create", 409, "Not ENROLLED"): lambda w: _exit(
        w, ENR_DONE, "cancel", {"cancellation_date": "2026-08-25"}
    ),
}
SPECIFIC.update(PHASE5_SPECIFIC)


# --- Phase 6 recipes: reconciliation (platform_admin; the world has no run, finding or snapshot)
RUNS, ANOMALIES, SNAPS = (
    "/api/v1/reconciliation/runs/",
    "/api/v1/anomalies/",
    "/api/v1/balance-snapshots/",
)
BILLING_OPERATIONS.update({"reconciliation_runs_list", "anomalies_list", "balance_snapshots_list"})

PHASE6_SPECIFIC = {
    ("reconciliation_runs_list", 400, "Invalid status filter"): lambda w: w.finance.get(
        RUNS, {"status": "DONE"}
    ),
    ("reconciliation_runs_create", 400, "STUDENT scope without student_id"): lambda w: (
        w.finance.post(RUNS, {"scope": "STUDENT", "as_of_date": "2026-12-31"}, format="json")
    ),
    ("reconciliation_runs_create", 400, "ALL scope with a student_id"): lambda w: w.finance.post(
        RUNS,
        {"scope": "ALL", "student_id": "STU-000001", "as_of_date": "2026-12-31"},
        format="json",
    ),
    ("reconciliation_runs_retrieve", 404, "Unknown ReconciliationRun"): lambda w: w.finance.get(
        f"{RUNS}RECON-2026-999999/"
    ),
    ("anomalies_list", 400, "Invalid issue_type filter"): lambda w: w.finance.get(
        ANOMALIES, {"issue_type": "LATE_PAYMENT"}
    ),
    ("anomalies_retrieve", 404, "Unknown AnomalyFinding"): lambda w: w.finance.get(
        f"{ANOMALIES}FND-2026-999999/"
    ),
    ("balance_snapshots_list", 400, "Invalid as_of_date filter"): lambda w: w.finance.get(
        SNAPS, {"as_of_date": "31-12-2026"}
    ),
    ("balance_snapshots_create", 400, "A server-computed field sent"): lambda w: w.finance.post(
        SNAPS, {"as_of_date": "2026-12-31", "reported_balance": "0.00"}, format="json"
    ),
    ("balance_snapshots_retrieve", 404, "Unknown BalanceSnapshot"): lambda w: w.finance.get(
        f"{SNAPS}SNAP-2026-999999/"
    ),
}
SPECIFIC.update(PHASE6_SPECIFIC)


# --- Phase 7 recipes: datagen (platform_admin; the world is not fresh and has no generation run)
GEN_RUNS = "/api/v1/datagen/runs/"
BILLING_OPERATIONS.add("datagen_runs_list")


def _generation_run():
    from django.utils import timezone

    from datagen.models import GenerationRun

    GenerationRun.objects.create(
        run_id="GEN-2026-0001",
        seed=20261022,
        scale_name="starter",
        scale_factor=Decimal("0.0100"),
        config_hash="0" * 64,
        row_counts={},
        checksums={},
        started_at=timezone.now(),
    )
    return f"{GEN_RUNS}GEN-2026-0001/evaluation/"


PHASE7_SPECIFIC = {
    ("datagen_runs_create", 400, "Full scale requested through the API"): lambda w: w.finance.post(
        GEN_RUNS, {"scale": "full"}, format="json"
    ),
    ("datagen_runs_create", 409, "Databases not fresh"): lambda w: w.finance.post(
        GEN_RUNS, {"scale": "starter"}, format="json"
    ),
    ("datagen_runs_retrieve", 404, "Unknown GenerationRun"): lambda w: w.finance.get(
        f"{GEN_RUNS}GEN-2026-9999/"
    ),
    ("datagen_runs_evaluation_retrieve", 404, "Unknown GenerationRun"): lambda w: w.finance.get(
        f"{GEN_RUNS}GEN-2026-9999/evaluation/", {"reconciliation_run": "RECON-2026-000001"}
    ),
    ("datagen_runs_evaluation_retrieve", 400, "reconciliation_run missing"): lambda w: (
        w.finance.get(_generation_run())
    ),
    ("datagen_runs_evaluation_retrieve", 400, "Unknown reconciliation run"): lambda w: (
        w.finance.get(_generation_run(), {"reconciliation_run": "RECON-2026-999999"})
    ),
}
SPECIFIC.update(PHASE7_SPECIFIC)


def _recipe(op_id, status, summary):
    if (op_id, status, summary) in SPECIFIC:
        return SPECIFIC[(op_id, status, summary)]
    method, path, body = OPERATIONS.get(op_id, (None, None, None))
    if status == 401 and summary == "Not authenticated" and method:
        return lambda w: getattr(APIClient(), method)(path, body, format="json")
    if status == 403 and summary == "Permission denied" and method:
        return lambda w: getattr(w.learner, method)(path, body, format="json")
    if status == 404 and summary == "Page past the last page" and op_id.endswith("_list"):
        # academic_admin may not read financial data (D6): billing lists use platform_admin
        return lambda w: (w.finance if op_id in BILLING_OPERATIONS else w.admin).get(
            path, {"page": 99}
        )
    return None


class World:
    pass


@pytest.fixture
def world(api_client_for, student, terms, program, monkeypatch):
    from academics import services
    from billing import services as billing
    from tests.billing_fixtures import make_fees

    enrollment = services.enroll(
        student=student,
        term=terms["TERM-2026-FALL"],
        credits=16,
        enrolled_on=date(2026, 8, 20),
        actor="t",
    )
    w = World()
    w.admin, w.learner = api_client_for("academic_admin"), api_client_for("learner")
    w.student, w.student_date = student, date(2026, 2, 1)
    assert (student.student_id, student.program_id) == ("STU-000001", "PRG-001")
    # Phase 2: FALL fee schedules, the FALL invoice, and a COMPLETED SPRING enrollment
    w.finance, w.monkeypatch = api_client_for("platform_admin"), monkeypatch
    w.fall, w.summer = terms["TERM-2026-FALL"], terms["TERM-2026-SUMMER"]
    make_fees(program, w.fall)
    w.invoice = billing.generate_invoice(enrollment=enrollment, actor="t")
    spring = services.enroll(
        student=student,
        term=terms["TERM-2026-SPRING"],
        credits=16,
        enrolled_on=date(2026, 1, 20),
        actor="t",
    )
    services.complete_enrollment(spring, on_date=date(2026, 5, 10), actor="t")
    assert (w.invoice.invoice_id, spring.enrollment_id) == (INV, "ENR-2026-000002")
    # Phase 3: the 7 documented types and STU-000001's FALL evaluation (ELG-2026-000001..7,
    # in priority order: SCT-007, SCT-001, …, SCT-006)
    from django.core.management import call_command

    from scholarships import services as scholarships

    call_command("seed_scholarship_types", verbosity=0)
    rows = scholarships.evaluate_eligibility(student=student, term=w.fall)
    assert [(e.pk, e.scholarship_type_id) for e in rows[:3]] == [
        ("ELG-2026-000001", "SCT-007"),
        ("ELG-2026-000002", "SCT-001"),
        ("ELG-2026-000003", "SCT-002"),
    ]
    return w


def test_documented_examples_were_found():
    # students 19 (list 4, create 3, retrieve 3, patch 4, status 5), programs 10, terms 10,
    # enrollments 16 (list 4, create 6 incl. two 422s, retrieve 3, events 3), health 405: 1
    # Phase 2 billing 56: fee-schedules list 4, create 5, new-version 6, history 3;
    # invoices list 4, retrieve 3, generate 7, void 7; late fees 3, refresh 3;
    # student ledger 4, statement 3, balance 4
    # Phase 3 scholarships 36: types list 4, create 4; eligibility checks 5, list 4;
    # scholarships create 6, list 4, retrieve 3, apply 6
    # Phase 4 payments and refunds 61: payments list 4, create 5, retrieve 3, allocate 8,
    # allocations 4; payment-allocations list 4; refunds list 4, create 6, retrieve 3,
    # approve 6, process 6, reject 5; student refundable 3
    # Phase 5: enrollments withdraw 6 and cancel 5; refunds create +1 (two 400s replace the
    # Phase 4 "Reason other than OVERPAYMENT" one, which is now valid input)
    # Phase 6 reconciliation 28: runs list 4, create 4, retrieve 3; anomalies list 4,
    # retrieve 3; balance snapshots list 4, create 3, retrieve 3
    # Phase 7 datagen 15: runs list 3, create 4, retrieve 3, evaluation 5
    assert len(EXAMPLES) == 56 + 56 + 36 + 61 + 12 + 28 + 15
    assert {op for op, *_ in EXAMPLES} >= set(OPERATIONS) | {"health_check"}


def test_every_example_has_a_replay():
    missing = [(op, st, name) for op, st, name, *_ in EXAMPLES if _recipe(op, st, name) is None]
    assert missing == []


@pytest.mark.parametrize(
    ("op_id", "status", "summary", "path", "method", "documented"),
    EXAMPLES,
    ids=[f"{e[0]}-{e[1]}-{e[2]}" for e in EXAMPLES],
)
def test_documented_example_equals_live_response(
    world, op_id, status, summary, path, method, documented
):
    r = _recipe(op_id, status, summary)(world)
    assert r.status_code == status, r.content
    assert r.json()["error"] == documented


RESOURCE_OF = {
    "students": "Student",
    "programs": "Program",
    "terms": "Term",
    "enrollments": "Enrollment",
    "fee-schedules": "FeeSchedule",
    "invoices": "Invoice",
    "scholarships": "Scholarship",
    "payments": "Payment",
    "refunds": "Refund",
    "reconciliation": "ReconciliationRun",
    "anomalies": "AnomalyFinding",
    "balance-snapshots": "BalanceSnapshot",
    "datagen": "GenerationRun",
}


def test_404_examples_name_the_resource_of_their_own_endpoint():
    for op_id, status, _, path, _, ex in EXAMPLES:
        if status == 404 and ex["message"] != "Invalid page.":
            model = RESOURCE_OF[path.split("/")[3]]
            assert ex["message"] == f"No {model} matches the given query.", op_id


def test_no_resource_reuses_another_resources_specific_example():
    """Specific (400/404/409/422) examples may be shared only within one resource."""
    owners = {}
    for _op_id, status, _, path, _, ex in EXAMPLES:
        if status in (400, 404, 409, 422) and ex["message"] != "Invalid page.":
            key = (status, repr(ex))
            owners.setdefault(key, set()).add(path.split("/")[3])
    shared = {k: v for k, v in owners.items() if len(v) > 1}
    assert shared == {}


@pytest.mark.parametrize("status", [400, 404, 405, 409, 422])
def test_error_responses_refuses_shared_defaults_for_specific_statuses(status):
    from core.schema import error_responses

    with pytest.raises(ValueError, match="endpoint-specific example"):
        error_responses(status)
    assert set(error_responses(401, 403)) == {401, 403}  # generic ones still allowed
