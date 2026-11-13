"""Shared fixtures. Domain objects are always created through services (as the API does)."""

import itertools
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from academics import services as academics_services
from academics.models import Term
from core.permissions import ALL_ROLES
from students import services as student_services

_email_seq = itertools.count(1)


@pytest.fixture
def terms(db):
    """The 12 documented terms, keyed by term_id."""
    call_command("seed_terms", verbosity=0)
    return {t.term_id: t for t in Term.objects.all()}


@pytest.fixture
def make_program(db):
    def _make(**overrides):
        fields = dict(
            code=f"BSC-{next(_email_seq):03d}",
            name="BSc Computer Science",
            level="UG",
            department="Computing",
            is_stem=True,
            has_lab=True,
            min_credits=12,
            max_credits=20,
            is_active=True,
        )
        fields.update(overrides)
        return academics_services.create_program(**fields)

    return _make


@pytest.fixture
def program(make_program):
    return make_program(code="BSC-CS")


@pytest.fixture
def make_student(db, terms, program):
    def _make(**overrides):
        n = next(_email_seq)
        fields = dict(
            first_name="Asha",
            last_name="Rao",
            email=f"student{n:06d}@example.edu",
            date_of_birth=date(2006, 3, 14),
            residency="DOMESTIC",
            program=program,
            admit_term=terms["TERM-2026-SPRING"],
            gpa=Decimal("3.72"),
            need_index=Decimal("0.35"),
            event_date=date(2026, 1, 5),
            actor="test",
        )
        fields.update(overrides)
        return student_services.create_student(**fields)

    return _make


@pytest.fixture
def student(make_student):
    return make_student()


@pytest.fixture
def api_client_for(db):
    """``api_client_for("academic_admin")`` → APIClient authenticated with a token."""

    def _client(role: str | None = None, *, superuser: bool = False) -> APIClient:
        username = f"{role or 'superuser'}_{next(_email_seq)}"
        user = (
            User.objects.create_superuser(username)
            if superuser
            else User.objects.create_user(username)
        )
        if role:
            assert role in ALL_ROLES
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}")
        return client

    return _client


@pytest.fixture
def admin_api(api_client_for):
    return api_client_for("academic_admin")


@pytest.fixture
def staff_api(api_client_for):
    return api_client_for("support_staff")


# --- verification helpers (Phase 1 API verification) -------------------------------


def _to_json_schema(node):
    """OpenAPI 3.0 schema -> JSON Schema (draft 7), strict about undocumented keys.

    * ``nullable: true`` becomes ``type: [T, "null"]`` (or anyOf with null)
    * object schemas with ``properties`` get ``additionalProperties: false`` unless the
      document says otherwise, so a response carrying an undocumented key fails
    """
    if isinstance(node, list):
        return [_to_json_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    drop = {
        "nullable",
        "readOnly",
        "writeOnly",
        "example",
        "examples",
        "discriminator",
        "xml",
        "externalDocs",
        "deprecated",
    }
    out = {k: _to_json_schema(v) for k, v in node.items() if k not in drop}
    if "properties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = False
    if node.get("nullable"):
        if "type" in out:
            out["type"] = [out["type"], "null"]
            if "enum" in out:
                out["enum"] = [*out["enum"], None]
        else:
            out = {"anyOf": [out, {"type": "null"}]}
    return out


@pytest.fixture(scope="session")
def openapi_doc():
    """The OpenAPI document exactly as served at /api/schema/."""
    import json

    from drf_spectacular.generators import SchemaGenerator
    from drf_spectacular.renderers import OpenApiJsonRenderer

    raw = SchemaGenerator().get_schema(request=None, public=True)
    return json.loads(OpenApiJsonRenderer().render(raw, renderer_context={}))


@pytest.fixture(scope="session")
def assert_matches_openapi(openapi_doc):
    """``assert_matches_openapi(response, "/api/v1/students/{student_id}/", "get")``."""
    from jsonschema import Draft7Validator

    components = _to_json_schema(openapi_doc["components"])

    def _check(response, path: str, method: str):
        op = openapi_doc["paths"][path][method.lower()]
        code = str(response.status_code)
        assert code in op["responses"], (
            f"{method.upper()} {path} returned {code}, which is not documented "
            f"(documented: {sorted(op['responses'])})"
        )
        schema = op["responses"][code]["content"]["application/json"]["schema"]
        validator = Draft7Validator({**_to_json_schema(schema), "components": components})
        errors = sorted(validator.iter_errors(response.json()), key=lambda e: list(e.path))
        assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])

    return _check


@pytest.fixture
def frozen_today(monkeypatch):
    """Freeze the API's business 'today' (used when a request omits a date)."""
    holder = {}

    def _freeze(d: date):
        holder["d"] = d
        for mod in ("students.views", "academics.views"):
            monkeypatch.setattr(f"{mod}.business_today", lambda: holder["d"])
        return d

    return _freeze
