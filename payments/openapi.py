"""Endpoint-specific error examples for the payments API. Every one is replayed against the
live API by tests/verification/test_verify_openapi_examples.py, in the verification world:
STU-000001 with the FALL invoice INV-STU-000001-2026-000001 (issued 2026-08-20, 164,000:
L01 TUITION 144,000, L02 LAB 6,000, L03 REGISTRATION 5,000, L04 TECHNOLOGY 4,000,
L05 LIBRARY 2,000, L06 STUDENT_ACTIVITY 3,000)."""

from core.schema import err, invalid_choice, list_error_responses, not_found

INVALID = "Request validation failed."
INV = "INV-STU-000001-2026-000001"
PAY = "PAY-2026-000001"
REF = "REF-2026-000001"

PAYMENT_LIST_ERRORS = list_error_responses(invalid_choice("payer_type", "PARENT"))
PAYMENT_404 = not_found("Payment")
PAYMENT_CREATE_400 = [
    (
        "Amount not positive",
        err("VALIDATION_ERROR", INVALID, {"amount": ["Must be greater than 0."]}),
    ),
    (
        "Received before the first invoice",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {
                "received_date": [
                    "Must be on or after the student's first invoice issue date (2026-08-20)."
                ]
            },
        ),
    ),
]
PAYMENT_CREATE_409 = (
    "Duplicate external_reference",
    err(
        "DUPLICATE_PAYMENT",
        f"external_reference 'UTR-0001' is already recorded as {PAY}.",
        {"external_reference": "UTR-0001", "existing_payment_id": PAY},
        rule_id="BR-PAY-01",
    ),
)

ALLOCATE_400 = (
    "MANUAL without lines",
    err("VALIDATION_ERROR", INVALID, {"lines": ["Required (1–99 lines) when mode is MANUAL."]}),
)
ALLOCATE_409 = (
    "Allocation ID limit reached",
    err(
        "ALLOCATION_LIMIT_REACHED",
        f"{PAY} already has 99 allocation rows; 1 more would exceed the 99 its allocation ID "
        "format allows.",
        {"payment_id": PAY, "existing_rows": 99, "requested_rows": 1, "limit": 99},
    ),
)
ALLOCATE_422 = [
    (
        "More than the payment's available amount",
        err(
            "ALLOCATION_EXCEEDS_PAYMENT",
            f"Allocating 50000.01 exceeds the 50000.00 available on {PAY}.",
            {"payment_id": PAY, "requested": "50000.01", "available": "50000.00"},
            rule_id="BR-ALC-01",
        ),
    ),
    (
        "More than the line's open amount",
        err(
            "ALLOCATION_EXCEEDS_LINE_OPEN",
            f"Allocating 6000.01 to {INV}-L02 exceeds its open amount 6000.00.",
            {
                "payment_id": PAY,
                "invoice_line_id": f"{INV}-L02",
                "amount": "6000.01",
                "line_open": "6000.00",
            },
            rule_id="BR-ALC-02",
        ),
    ),
    (
        "Another student's line",
        err(
            "ALLOCATION_STUDENT_MISMATCH",
            f"{PAY} belongs to STU-000001; INV-STU-000002-2026-000001-L01 belong(s) to another "
            "student.",
            {
                "payment_id": PAY,
                "student_id": "STU-000001",
                "invoice_line_ids": ["INV-STU-000002-2026-000001-L01"],
            },
        ),
    ),
]
ALLOCATION_LIST_ERRORS = list_error_responses(invalid_choice("allocation_type", "REVERSE"))

REFUND_LIST_ERRORS = list_error_responses(invalid_choice("status", "PAID"))
REFUND_404 = not_found("Refund")
REFUND_CREATE_400 = (
    "Reason other than OVERPAYMENT",
    err("VALIDATION_ERROR", INVALID, {"reason": ['"WITHDRAWAL" is not a valid choice.']}),
)


def _caps(amount, net_paid, ledger, credit, pending, credit_available, available, maximum):
    return {
        "payment_id": PAY,
        "amount": amount,
        "net_paid": net_paid,
        "ledger_balance": ledger,
        "credit_balance": credit,
        "pending_refunds": pending,
        "credit_available": credit_available,
        "payment_available": available,
        "max_refundable": maximum,
    }


REFUND_CREATE_422 = [
    (
        "More than net paid",
        err(
            "REFUND_EXCEEDS_NET_PAID",
            "Refund 89000.01 exceeds the student's net paid 89000.00 (payments less refunds not "
            "rejected).",
            _caps(
                "89000.01",
                "89000.00",
                "-140000.00",
                "140000.00",
                "0.00",
                "140000.00",
                "89000.00",
                "89000.00",
            ),
            rule_id="BR-REF-01",
        ),
    ),
    (
        "More than the credit available",
        err(
            "REFUND_EXCEEDS_CREDIT_BALANCE",
            "Refund 6000.00 exceeds the credit available for refunds 5000.00 (credit balance "
            "10000.00 less 5000.00 reserved by pending refunds).",
            _caps(
                "6000.00",
                "169000.00",
                "-10000.00",
                "10000.00",
                "5000.00",
                "5000.00",
                "19000.00",
                "5000.00",
            ),
            rule_id="BR-REF-02",
        ),
    ),
    (
        "More than the payment's available amount",
        err(
            "REFUND_EXCEEDS_PAYMENT_AVAILABLE",
            f"Refund 30000.01 exceeds the 30000.00 available on {PAY}.",
            _caps(
                "30000.01",
                "204000.00",
                "-40000.00",
                "40000.00",
                "0.00",
                "40000.00",
                "30000.00",
                "30000.00",
            ),
            rule_id="BR-REF-02",
        ),
    ),
]
APPROVE_400 = (
    "on_date before the request",
    err(
        "VALIDATION_ERROR",
        INVALID,
        {"on_date": ["Must be on or after 2026-08-21, the refund's latest status date."]},
    ),
)
APPROVE_409 = (
    "Already approved",
    err(
        "ILLEGAL_TRANSITION",
        "Refund cannot move from APPROVED to APPROVED.",
        {
            "entity": "Refund",
            "record_id": REF,
            "from_status": "APPROVED",
            "to_status": "APPROVED",
            "allowed": ["PROCESSED", "REJECTED"],
        },
        rule_id="BR-REF-03",
    ),
)
APPROVE_422 = (
    "Credit used up since the request (late fee)",
    err(
        "REFUND_EXCEEDS_CREDIT_BALANCE",
        "Refund 10000.00 exceeds the credit available for refunds 9500.00 (credit balance "
        "9500.00 less 0.00 reserved by pending refunds).",
        {
            "refund_id": REF,
            **_caps(
                "10000.00",
                "174000.00",
                "-9500.00",
                "9500.00",
                "0.00",
                "9500.00",
                "24000.00",
                "9500.00",
            ),
        },
        rule_id="BR-REF-02",
    ),
)
PROCESS_400 = (
    "processed_date missing",
    err("VALIDATION_ERROR", INVALID, {"processed_date": ["This field is required."]}),
)
PROCESS_409 = (
    "Not approved yet",
    err(
        "ILLEGAL_TRANSITION",
        "Refund cannot move from REQUESTED to PROCESSED.",
        {
            "entity": "Refund",
            "record_id": REF,
            "from_status": "REQUESTED",
            "to_status": "PROCESSED",
            "allowed": ["APPROVED", "REJECTED"],
        },
        rule_id="BR-REF-03",
    ),
)
REJECT_400 = (
    "on_date missing",
    err("VALIDATION_ERROR", INVALID, {"on_date": ["This field is required."]}),
)
REJECT_409 = (
    "Already processed",
    err(
        "ILLEGAL_TRANSITION",
        "Refund cannot move from PROCESSED to REJECTED.",
        {
            "entity": "Refund",
            "record_id": REF,
            "from_status": "PROCESSED",
            "to_status": "REJECTED",
            "allowed": [],
        },
        rule_id="BR-REF-03",
    ),
)
STUDENT_404 = not_found("Student")
PROCESS_422 = (
    "Credit used up since approval (late fee)",
    APPROVE_422[1],
)
