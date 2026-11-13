"""V-HEALTH: /api/v1/health/ with both databases up, each one down, and both down.

Outages are simulated at the connection level (the cursor raises OperationalError), so
the real probe code path runs. Every response is validated against the OpenAPI schema.
"""

from unittest import mock

import pytest
from django.db import OperationalError, connections
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db(databases="__all__")
H = "/api/v1/health/"


def _down(*aliases):
    stack = []
    for a in aliases:
        p = mock.patch.object(
            connections[a], "cursor", side_effect=OperationalError("unable to open database file")
        )
        stack.append(p)
    return stack


def _get(*down):
    patches = _down(*down)
    for p in patches:
        p.start()
    try:
        return APIClient().get(H)
    finally:
        for p in patches:
            p.stop()


def test_both_available_200(assert_matches_openapi):
    r = _get()
    assert_matches_openapi(r, H, "get")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok" and set(b["databases"]) == {"default", "ground_truth"}
    assert b["databases"]["default"] == {
        "status": "ok",
        "journal_mode": "wal",
        "foreign_keys": True,
        "transaction_mode": "IMMEDIATE",
    }
    assert b["databases"]["ground_truth"] == {
        "status": "ok",
        "journal_mode": "delete",
        "foreign_keys": True,
        "transaction_mode": "IMMEDIATE",
    }


@pytest.mark.parametrize(
    ("down", "up"), [(("default",), "ground_truth"), (("ground_truth",), "default")]
)
def test_one_database_unavailable_503(assert_matches_openapi, down, up):
    r = _get(*down)
    assert_matches_openapi(r, H, "get")
    assert r.status_code == 503 and r.json()["status"] == "degraded"
    dbs = r.json()["databases"]
    assert dbs[down[0]] == {
        "status": "error",
        "journal_mode": None,
        "foreign_keys": None,
        "transaction_mode": None,
        "error": "unable to open database file",
    }
    assert dbs[up]["status"] == "ok"


def test_both_unavailable_503(assert_matches_openapi):
    r = _get("default", "ground_truth")
    assert_matches_openapi(r, H, "get")
    assert r.status_code == 503
    assert {v["status"] for v in r.json()["databases"].values()} == {"error"}


def test_public_and_ignores_bad_credentials():
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")
    assert c.get(H).status_code == 200  # no authentication classes on health


def test_live_body_matches_documented_examples(openapi_doc):
    responses = openapi_doc["paths"][H]["get"]["responses"]
    live_ok, live_bad = _get().json(), _get("ground_truth").json()
    doc_ok = next(iter(responses["200"]["content"]["application/json"]["examples"].values()))
    doc_bad = next(iter(responses["503"]["content"]["application/json"]["examples"].values()))
    for live, doc in ((live_ok, doc_ok["value"]), (live_bad, doc_bad["value"])):
        assert set(live) == set(doc)
        assert {a: set(v) for a, v in live["databases"].items()} == {
            a: set(v) for a, v in doc["databases"].items()
        }
        assert live["status"] == doc["status"]
        assert {a: v["status"] for a, v in live["databases"].items()} == {
            a: v["status"] for a, v in doc["databases"].items()
        }
    assert live_ok["databases"] == doc_ok["value"]["databases"]


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_writes_405(method):
    r = getattr(APIClient(), method)(H, {}, format="json")
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
