"""Health check (ARCHITECTURE.md §9.2: ``GET /api/v1/health/``).

Lives in ``config`` (project wiring), not ``core``: it probes every configured database
and its documentation names both aliases, which ``core`` must never do (§12.2). The probe
only runs ``SELECT 1`` and PRAGMAs; it reads no table in either database.
"""

from __future__ import annotations

import sqlite3

import django
from django.conf import settings
from django.db import DatabaseError, connections
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.schema import error_responses, method_not_allowed
from core.serializers import HealthSerializer


def _probe(alias: str) -> dict:
    conn = connections[alias]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.execute("PRAGMA journal_mode")
            journal_mode = cur.fetchone()[0]
            cur.execute("PRAGMA foreign_keys")
            foreign_keys = bool(cur.fetchone()[0])
        return {
            "status": "ok",
            "journal_mode": journal_mode,
            "foreign_keys": foreign_keys,
            "transaction_mode": conn.settings_dict.get("OPTIONS", {}).get("transaction_mode"),
        }
    except DatabaseError as exc:
        return {
            "status": "error",
            "journal_mode": None,
            "foreign_keys": None,
            "transaction_mode": None,
            "error": str(exc),
        }


# Examples mirror the real body for the two configured aliases: "default" uses WAL
# (init_command); the label database keeps SQLite's default "delete" journal mode.
_BASE = {
    "service": "student-finance",
    "version": "0.1.0",
    "api_version": "v1",
    "django_version": "5.2.19",
    "sqlite_version": "3.45.1",
}
_DEFAULT_OK = {
    "status": "ok",
    "journal_mode": "wal",
    "foreign_keys": True,
    "transaction_mode": "IMMEDIATE",
}
HEALTHY_EXAMPLE = {
    "status": "ok",
    **_BASE,
    "databases": {
        "default": _DEFAULT_OK,
        "ground_truth": {
            "status": "ok",
            "journal_mode": "delete",
            "foreign_keys": True,
            "transaction_mode": "IMMEDIATE",
        },
    },
}
DEGRADED_EXAMPLE = {
    "status": "degraded",
    **_BASE,
    "databases": {
        "default": _DEFAULT_OK,
        "ground_truth": {
            "status": "error",
            "journal_mode": None,
            "foreign_keys": None,
            "transaction_mode": None,
            "error": "unable to open database file",
        },
    },
}


class HealthView(APIView):
    """Liveness + database connectivity. Public, unauthenticated, read-only."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="health_check",
        summary="Service health",
        description=(
            "Probes every configured SQLite database (`default` and `ground_truth`) with "
            "`SELECT 1` and reports its journal mode, foreign-key enforcement and "
            "transaction mode. Returns **200** when all answer and **503** with "
            "`status: degraded` when any fails. No table is read."
        ),
        responses={
            200: OpenApiResponse(
                HealthSerializer,
                description="Every database answered",
                examples=[
                    OpenApiExample(
                        "Healthy", value=HEALTHY_EXAMPLE, status_codes=["200"], response_only=True
                    )
                ],
            ),
            503: OpenApiResponse(
                HealthSerializer,
                description="At least one database failed its probe",
                examples=[
                    OpenApiExample(
                        "Degraded", value=DEGRADED_EXAMPLE, status_codes=["503"], response_only=True
                    )
                ],
            ),
            **error_responses(405, examples={405: method_not_allowed("POST")}),
        },
        tags=["system"],
    )
    def get(self, request):
        databases = {alias: _probe(alias) for alias in connections}
        healthy = all(db["status"] == "ok" for db in databases.values())
        body = {
            "status": "ok" if healthy else "degraded",
            "service": "student-finance",
            "version": settings.SPECTACULAR_SETTINGS["VERSION"],
            "api_version": "v1",
            "django_version": django.get_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "databases": databases,
        }
        return Response(body, status=200 if healthy else 503)
