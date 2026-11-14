"""Endpoint-specific error examples for the datagen API. Every one is replayed against the
live API by tests/verification/test_verify_openapi_examples.py (verification world:
STU-000001 with its FALL and SPRING enrollments, the FALL invoice, the 7 scholarship types and
STU-000001's FALL evaluation; no generation run)."""

from core.schema import INVALID_PAGE, err, error_responses, not_found

INVALID = "Request validation failed."

RUN_LIST_ERRORS = error_responses(401, 403, 404, examples={404: INVALID_PAGE})
RUN_404 = not_found("GenerationRun")

RUN_CREATE_400 = [
    (
        "Full scale requested through the API",
        err("VALIDATION_ERROR", INVALID, {"scale": ['"full" is not a valid choice.']}),
    ),
]
# the verification world is not fresh: its exact non-empty tables (decision D-G6)
NOT_FRESH_TABLES = [
    "academics_enrollment",
    "academics_enrollmentevent",
    "academics_program",
    "academics_term",
    "billing_feeschedule",
    "billing_invoice",
    "billing_invoiceevent",
    "billing_invoiceline",
    "billing_ledgerentry",
    "core_idsequence",
    "scholarships_eligibility",
    "scholarships_scholarshiptype",
    "scholarships_scholarshiptypeprogram",
    "students_student",
    "students_studentstatusevent",
]
RUN_CREATE_409 = (
    "Databases not fresh",
    err(
        "DATASET_NOT_FRESH",
        "Generation needs fresh, migrated databases; these tables already hold rows: "
        + ", ".join(NOT_FRESH_TABLES)
        + ".",
        {"non_empty_tables": NOT_FRESH_TABLES},
    ),
)
EVALUATION_400 = [
    (
        "reconciliation_run missing",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"reconciliation_run": ["This query parameter is required."]},
        ),
    ),
    (
        "Unknown reconciliation run",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"reconciliation_run": ["Unknown reconciliation run RECON-2026-999999."]},
        ),
    ),
]
