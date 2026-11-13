"""Reusable OpenAPI fragments for drf-spectacular (error envelope responses).

Only responses that are truly identical on every endpoint have shared examples: 401, 403
and 405. Every 400, 404, 409 and 422 example must be written for its own endpoint
(``error_responses`` refuses to fall back to a default), because a shared default is what
let a Student example appear on Program, Term and Enrollment endpoints.

``tests/verification/test_verify_openapi_examples.py`` replays every documented error
example against the live API and requires the response to equal the example exactly.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiExample, OpenApiResponse

from core.serializers import ErrorEnvelopeSerializer

_DESCRIPTIONS = {
    400: "Validation error (malformed input, unknown field, bad enum, bad date order)",
    401: "Authentication credentials were not provided or are invalid",
    403: "Your role does not allow this action",
    404: "No record with this ID (detail), or a page number past the last page (list)",
    405: "HTTP method not allowed on this endpoint",
    409: "State conflict (duplicate record or illegal lifecycle transition)",
    422: "Request is well formed but breaks a business rule",
}


def err(code: str, message: str, details: dict | None = None, rule_id: str | None = None) -> dict:
    """The ``error`` object of the envelope, as an example value."""
    return {"code": code, "rule_id": rule_id, "message": message, "details": details or {}}


# Identical on every endpoint, so they may be shared.
GENERIC: dict[int, tuple[str, dict]] = {
    401: (
        "Not authenticated",
        err("NOT_AUTHENTICATED", "Authentication credentials were not provided."),
    ),
    403: ("Permission denied", err("PERMISSION_DENIED", "Your role does not allow this action.")),
}
SPECIFIC_REQUIRED = frozenset({400, 404, 405, 409, 422})

INVALID_PAGE = ("Page past the last page", err("NOT_FOUND", "Invalid page."))


def not_found(model_name: str) -> tuple[str, dict]:
    """The 404 a detail endpoint returns for an unknown ID (Django's get_object_or_404 text)."""
    return (f"Unknown {model_name}", err("NOT_FOUND", f"No {model_name} matches the given query."))


def method_not_allowed(method: str) -> tuple[str, dict]:
    return (f"{method} not allowed", err("METHOD_NOT_ALLOWED", f'Method "{method}" not allowed.'))


Example = tuple[str, dict]


def error_responses(
    *statuses: int,
    examples: dict[int, Example | list[Example]] | None = None,
) -> dict:
    """``{status: OpenApiResponse}`` documenting the error envelope for each status.

    ``examples[status]`` is ``(name, error)`` or a list of them. 401 and 403 fall back to
    GENERIC; any other status without an endpoint-specific example raises ValueError.
    """
    examples = examples or {}
    out = {}
    for status in statuses:
        given = examples.get(status) or GENERIC.get(status)
        if given is None:
            raise ValueError(f"HTTP {status} needs an endpoint-specific example")
        named = given if isinstance(given, list) else [given]
        out[status] = OpenApiResponse(
            ErrorEnvelopeSerializer,
            description=_DESCRIPTIONS[status],
            examples=[
                OpenApiExample(
                    name, value={"error": value}, status_codes=[str(status)], response_only=True
                )
                for name, value in named
            ],
        )
    return out


def list_error_responses(filter_example: Example) -> dict:
    """Errors a paginated, filtered list can return: 400 (bad filter), 401, 403, 404 (page)."""
    return error_responses(400, 401, 403, 404, examples={400: filter_example, 404: INVALID_PAGE})


def invalid_choice(field: str, value: str) -> Example:
    """400 from a django-filter ChoiceFilter given a value outside its choices."""
    return (
        f"Invalid {field} filter",
        err(
            "VALIDATION_ERROR",
            "Request validation failed.",
            {field: [f"Select a valid choice. {value} is not one of the available choices."]},
        ),
    )
