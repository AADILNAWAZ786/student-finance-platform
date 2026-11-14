"""Endpoint-specific error examples for the reconciliation API. Every one is replayed against
the live API by tests/verification/test_verify_openapi_examples.py (verification world:
STU-000001, no run, finding or snapshot yet)."""

from core.schema import err, invalid_choice, list_error_responses, not_found

INVALID = "Request validation failed."

RUN_LIST_ERRORS = list_error_responses(invalid_choice("status", "DONE"))
FINDING_LIST_ERRORS = list_error_responses(invalid_choice("issue_type", "LATE_PAYMENT"))
SNAPSHOT_LIST_ERRORS = list_error_responses(
    (
        "Invalid as_of_date filter",
        err("VALIDATION_ERROR", INVALID, {"as_of_date": ["Enter a valid date."]}),
    )
)

RUN_404 = not_found("ReconciliationRun")
FINDING_404 = not_found("AnomalyFinding")
SNAPSHOT_404 = not_found("BalanceSnapshot")

RUN_CREATE_400 = [
    (
        "STUDENT scope without student_id",
        err("VALIDATION_ERROR", INVALID, {"student_id": ["Required when scope is STUDENT."]}),
    ),
    (
        "ALL scope with a student_id",
        err("VALIDATION_ERROR", INVALID, {"student_id": ["Must be omitted when scope is ALL."]}),
    ),
]
# (an as_of_date outside 2026 is also a 400, with the same body as every billing endpoint's,
# so it is not repeated here: a specific example may belong to one resource only)
SNAPSHOT_CREATE_400 = [
    (
        "A server-computed field sent",
        err("VALIDATION_ERROR", INVALID, {"reported_balance": ["Unknown field."]}),
    ),
]
