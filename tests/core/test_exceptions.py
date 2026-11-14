"""Error envelope (ARCHITECTURE.md §9.1) and the domain error registry (§10)."""

import pytest
from django.http import Http404
from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from core.exceptions import (
    DOCUMENTED_DOMAIN_ERROR_CODES,
    DOMAIN_ERROR_CODES,
    EXTENSION_DOMAIN_ERROR_CODES,
    BusinessRuleViolation,
    DomainError,
    StateConflict,
    domain_error,
)

pytestmark = pytest.mark.phase0
factory = APIRequestFactory()


def test_registry_has_the_18_documented_codes():
    assert len(DOCUMENTED_DOMAIN_ERROR_CODES) == 18
    assert {c for c, s in DOCUMENTED_DOMAIN_ERROR_CODES.items() if s == 409} == {
        "DUPLICATE_INVOICE",
        "DUPLICATE_PAYMENT",
        "ENROLLMENT_EXISTS",
        "INVOICE_HAS_ALLOCATIONS",
        "SCHOLARSHIP_GROUP_CONFLICT",
        "ILLEGAL_TRANSITION",
    }
    assert set(DOMAIN_ERROR_CODES.values()) == {409, 422}


def test_documented_codes_are_exactly_the_frozen_catalogue():
    """ARCHITECTURE §10 lists the domain codes in one line; the registry matches it."""
    import re
    from pathlib import Path

    from django.conf import settings

    text = (Path(settings.BASE_DIR) / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if x.startswith("Domain error codes:"))
    assert set(re.findall(r"`([A-Z_]+)`", line)) == set(DOCUMENTED_DOMAIN_ERROR_CODES)


def test_the_only_extension_is_the_approved_phase4_code():
    """D-P13a / P4-O2 and D-G6 (Phase 7): the two approved exceptions to the 18-code
    catalogue, nothing else."""
    assert EXTENSION_DOMAIN_ERROR_CODES == {
        "ALLOCATION_LIMIT_REACHED": 409,
        "DATASET_NOT_FRESH": 409,  # Phase 7, decision D-G6
    }
    assert DOMAIN_ERROR_CODES == {**DOCUMENTED_DOMAIN_ERROR_CODES, **EXTENSION_DOMAIN_ERROR_CODES}
    assert len(DOMAIN_ERROR_CODES) == 20
    e = domain_error("ALLOCATION_LIMIT_REACHED", "full")
    assert isinstance(e, StateConflict) and e.status_code == 409


def test_domain_error_picks_subclass_and_status():
    e = domain_error("DUPLICATE_INVOICE", "exists", rule_id="BR-INV-01")
    assert isinstance(e, StateConflict) and e.status_code == 409
    e = domain_error("REFUND_EXCEEDS_NET_PAID", "too much", rule_id="BR-REF-01")
    assert isinstance(e, BusinessRuleViolation) and e.status_code == 422


def test_unregistered_code_is_a_programming_error():
    with pytest.raises(ValueError):
        domain_error("SOMETHING_NEW", "x")
    with pytest.raises(ValueError):
        DomainError("SOMETHING_NEW", "x")


class _Body(serializers.Serializer):
    amount = serializers.CharField()


class _Boom(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    exc: Exception | None = None

    def get(self, request):
        raise self.exc

    def post(self, request):
        _Body(data=request.data).is_valid(raise_exception=True)


class _Protected(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):  # pragma: no cover - never reached unauthenticated
        return None


def _call(exc):
    return _Boom.as_view(exc=exc)(factory.get("/x"))


def test_domain_error_rendered_as_envelope_409():
    r = _call(
        domain_error(
            "DUPLICATE_INVOICE",
            "Invoice already exists.",
            rule_id="BR-INV-01",
            details={"existing_invoice_id": "INV-STU-000001-2026-000001"},
        )
    )
    assert r.status_code == 409
    assert r.data == {
        "error": {
            "code": "DUPLICATE_INVOICE",
            "rule_id": "BR-INV-01",
            "message": "Invoice already exists.",
            "details": {"existing_invoice_id": "INV-STU-000001-2026-000001"},
        }
    }


def test_domain_error_rendered_as_envelope_422():
    r = _call(domain_error("ALLOCATION_EXCEEDS_PAYMENT", "over", rule_id="BR-ALC-01"))
    assert r.status_code == 422
    assert r.data["error"]["code"] == "ALLOCATION_EXCEEDS_PAYMENT"
    assert r.data["error"]["rule_id"] == "BR-ALC-01"


def test_validation_error_400_envelope():
    r = _Boom.as_view()(factory.post("/x", {}, format="json"))
    assert r.status_code == 400
    err = r.data["error"]
    assert err["code"] == "VALIDATION_ERROR" and err["rule_id"] is None
    assert "amount" in err["details"]


def test_django_404_becomes_envelope():
    r = _call(Http404())
    assert r.status_code == 404 and r.data["error"]["code"] == "NOT_FOUND"


def test_unauthenticated_401_envelope_keeps_header():
    r = _Protected.as_view()(factory.get("/x"))
    assert r.status_code == 401
    assert r.data["error"]["code"] == "NOT_AUTHENTICATED"
    assert r.has_header("WWW-Authenticate")


def test_unexpected_exceptions_are_not_swallowed():
    with pytest.raises(ZeroDivisionError):
        _call(ZeroDivisionError("bug"))
