"""Domain errors and the error-code registry (ARCHITECTURE.md §9.1, §10).

Services raise ``domain_error(code, message, rule_id=..., details=...)``. The API
exception handler (``core.api.exception_handler``) turns it into the envelope

    {"error": {"code": ..., "rule_id": ..., "message": ..., "details": {...}}}

with the HTTP status registered for that code.
"""

from __future__ import annotations

from typing import Any

# Domain error codes -> HTTP status (ARCHITECTURE §10). 409 = state conflict,
# 422 = valid request that breaks a financial rule.
DOMAIN_ERROR_CODES: dict[str, int] = {
    "DUPLICATE_INVOICE": 409,
    "DUPLICATE_PAYMENT": 409,
    "FEE_SCHEDULE_NOT_FOUND": 422,
    "FEE_SCHEDULE_VERSION_INVALID": 422,
    "CREDITS_OUT_OF_RANGE": 422,
    "ENROLLMENT_EXISTS": 409,
    "TERM_NOT_BILLABLE": 422,
    "INVOICE_HAS_ALLOCATIONS": 409,
    "SCHOLARSHIP_NOT_ELIGIBLE": 422,
    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES": 422,
    "SCHOLARSHIP_GROUP_CONFLICT": 409,
    "ALLOCATION_EXCEEDS_PAYMENT": 422,
    "ALLOCATION_EXCEEDS_LINE_OPEN": 422,
    "ALLOCATION_STUDENT_MISMATCH": 422,
    "REFUND_EXCEEDS_NET_PAID": 422,
    "REFUND_EXCEEDS_CREDIT_BALANCE": 422,
    "REFUND_EXCEEDS_PAYMENT_AVAILABLE": 422,
    "ILLEGAL_TRANSITION": 409,
}

# Infrastructure codes used by the exception handler for non-domain errors.
API_ERROR_CODES: dict[str, int] = {
    "VALIDATION_ERROR": 400,
    "PARSE_ERROR": 400,
    "NOT_AUTHENTICATED": 401,
    "AUTHENTICATION_FAILED": 401,
    "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "NOT_ACCEPTABLE": 406,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "THROTTLED": 429,
}


class DomainError(Exception):
    """Base for business-rule failures raised by services."""

    status_code = 422

    def __init__(
        self,
        code: str,
        message: str,
        *,
        rule_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in DOMAIN_ERROR_CODES:
            raise ValueError(f"unregistered domain error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.rule_id = rule_id
        self.details = dict(details or {})
        self.status_code = DOMAIN_ERROR_CODES[code]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "rule_id": self.rule_id,
            "message": self.message,
            "details": self.details,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.code!r}, {self.message!r}, rule_id={self.rule_id!r})"


class BusinessRuleViolation(DomainError):
    """422: the request is well formed but breaks a financial rule."""


class StateConflict(DomainError):
    """409: the request conflicts with the current state (duplicate, illegal transition)."""


def domain_error(
    code: str,
    message: str,
    *,
    rule_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> DomainError:
    """Build the right DomainError subclass for a registered code."""
    if code not in DOMAIN_ERROR_CODES:
        raise ValueError(f"unregistered domain error code {code!r}")
    cls = StateConflict if DOMAIN_ERROR_CODES[code] == 409 else BusinessRuleViolation
    return cls(code, message, rule_id=rule_id, details=details)
