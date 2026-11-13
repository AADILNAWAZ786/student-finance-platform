"""Shared API infrastructure: error envelope, pagination (ARCHITECTURE.md §9.1)."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.urls import Resolver404, resolve
from rest_framework import exceptions
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from core.exceptions import DomainError

_DRF_CODES: list[tuple[type[exceptions.APIException], str]] = [
    (exceptions.ValidationError, "VALIDATION_ERROR"),
    (exceptions.ParseError, "PARSE_ERROR"),
    (exceptions.NotAuthenticated, "NOT_AUTHENTICATED"),
    (exceptions.AuthenticationFailed, "AUTHENTICATION_FAILED"),
    (exceptions.PermissionDenied, "PERMISSION_DENIED"),
    (exceptions.NotFound, "NOT_FOUND"),
    (exceptions.MethodNotAllowed, "METHOD_NOT_ALLOWED"),
    (exceptions.NotAcceptable, "NOT_ACCEPTABLE"),
    (exceptions.UnsupportedMediaType, "UNSUPPORTED_MEDIA_TYPE"),
    (exceptions.Throttled, "THROTTLED"),
]


def envelope(
    code: str, message: str, *, rule_id: str | None = None, details: Any = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "rule_id": rule_id,
            "message": message,
            "details": details if details is not None else {},
        }
    }


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Render every handled error as ``{"error": {code, rule_id, message, details}}``.

    Unhandled exceptions return ``None`` so Django produces a 500 (they are bugs,
    not API errors, and must not be swallowed).
    """
    if isinstance(exc, DomainError):
        return Response({"error": exc.as_dict()}, status=exc.status_code)

    if isinstance(exc, Http404):
        exc = exceptions.NotFound(str(exc) or None)
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()
    elif isinstance(exc, DjangoValidationError):
        # raised by services and model.full_clean(); same 400 envelope as serializer errors
        exc = exceptions.ValidationError(
            exc.message_dict if hasattr(exc, "error_dict") else {"non_field_errors": exc.messages}
        )

    response = drf_exception_handler(exc, context)  # keeps headers (WWW-Authenticate, Allow, …)
    if response is None or not isinstance(exc, exceptions.APIException):
        return response

    code = next((c for cls, c in _DRF_CODES if isinstance(exc, cls)), "API_ERROR")
    if isinstance(exc, exceptions.ValidationError):
        message, details = "Request validation failed.", exc.detail
    else:
        message, details = str(exc.detail), {}
    response.data = envelope(code, message, details=details)
    return response


def api_not_found(request, *args, **kwargs) -> HttpResponse:
    """Catch-all for unmatched ``/api/...`` URLs: the JSON 404 envelope, never an HTML page.

    Registered last in ``config.urls``. Because this route matches every /api/ path,
    CommonMiddleware's APPEND_SLASH redirect no longer fires on its own, so the same
    check is done here: ``/api/v1/students`` → 301 ``/api/v1/students/``.
    """
    if settings.APPEND_SLASH and not request.path_info.endswith("/"):
        try:
            match = resolve(request.path_info + "/")
        except Resolver404:
            match = None
        if match is not None and match.url_name != "api-not-found":
            return HttpResponsePermanentRedirect(request.get_full_path(force_append_slash=True))
    return JsonResponse(
        envelope("NOT_FOUND", f"No API endpoint matches {request.path}."), status=404
    )


class StandardPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 500
