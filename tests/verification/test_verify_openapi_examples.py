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
    if op.startswith(("fee_", "invoices_", "billing_", "scholarship", "eligibility"))
} | {
    "students_ledger_list",
    "students_statement_retrieve",
    "students_balance_retrieve",
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
    assert len(EXAMPLES) == 56 + 56 + 36
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
