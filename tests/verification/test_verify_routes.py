"""V-ROUTE: every Phase 1 and Phase 2 URL × every HTTP method (actual routes from the URL resolver).

Allowed methods must not return 405; every other method must return the documented
405 envelope with an ``Allow`` header. Unknown or malformed URLs under /api/ must also
use the error envelope (ARCHITECTURE §9.1: every 4xx uses the envelope).
"""

import pytest

# the health route probes every configured database, so these tests may touch both
pytestmark = pytest.mark.django_db(databases="__all__")

FEE = "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
INV = "INV-STU-000001-2026-000001"

# (route template, concrete path, allowed methods besides HEAD/OPTIONS)
ROUTES = [
    ("health", "/api/v1/health/", {"GET"}),
    ("students", "/api/v1/students/", {"GET", "POST"}),
    ("student", "/api/v1/students/STU-000001/", {"GET", "PATCH"}),
    ("student-status", "/api/v1/students/STU-000001/status/", {"POST"}),
    ("programs", "/api/v1/programs/", {"GET", "POST"}),
    ("program", "/api/v1/programs/PRG-001/", {"GET"}),
    ("terms", "/api/v1/terms/", {"GET", "POST"}),
    ("term", "/api/v1/terms/TERM-2026-FALL/", {"GET"}),
    ("enrollments", "/api/v1/enrollments/", {"GET", "POST"}),
    ("enrollment", "/api/v1/enrollments/ENR-2026-000001/", {"GET"}),
    ("enrollment-events", "/api/v1/enrollments/ENR-2026-000001/events/", {"GET"}),
    # Phase 2: billing (financial records are read + action only; no fee-schedule detail, D14)
    ("fee-schedules", "/api/v1/fee-schedules/", {"GET", "POST"}),
    ("fee-schedule-new-version", f"/api/v1/fee-schedules/{FEE}/new-version/", {"POST"}),
    ("fee-schedule-history", f"/api/v1/fee-schedules/{FEE}/history/", {"GET"}),
    ("invoices", "/api/v1/invoices/", {"GET"}),
    ("invoices-generate", "/api/v1/invoices/generate/", {"POST"}),
    ("invoice", f"/api/v1/invoices/{INV}/", {"GET"}),
    ("invoice-void", f"/api/v1/invoices/{INV}/void/", {"POST"}),
    ("late-fees-assess", "/api/v1/billing/late-fees/assess/", {"POST"}),
    ("collection-status-refresh", "/api/v1/billing/collection-status/refresh/", {"POST"}),
    ("student-ledger", "/api/v1/students/STU-000001/ledger/", {"GET"}),
    ("student-statement", "/api/v1/students/STU-000001/statement/", {"GET"}),
    ("student-balance", "/api/v1/students/STU-000001/balance/", {"GET"}),
]
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
DISALLOWED = [
    (name, path, m, allowed) for name, path, allowed in ROUTES for m in METHODS if m not in allowed
]


@pytest.fixture
def world(api_client_for, student, terms, program):
    from academics import services
    from billing import services as billing
    from tests.billing_fixtures import make_fees

    enrollment = services.enroll(
        student=student,
        term=terms["TERM-2026-FALL"],
        credits=16,
        enrolled_on=terms["TERM-2026-FALL"].start_date,
        actor="t",
    )
    make_fees(program, terms["TERM-2026-FALL"])  # Phase 2: fee schedules and an invoice
    billing.generate_invoice(enrollment=enrollment, actor="t")
    return api_client_for("platform_admin")


def test_route_table_matches_resolver():
    """The table above is the real URL configuration (no route missing, none extra)."""
    from django.urls import URLResolver, get_resolver

    def walk(r, prefix=""):
        for p in r.url_patterns:
            if isinstance(p, URLResolver):
                yield from walk(p, prefix + str(p.pattern))
            else:
                yield prefix + str(p.pattern)

    api = sorted(r for r in walk(get_resolver()) if r.startswith("api/v1/"))
    assert len(api) == len(ROUTES) == 23  # Phase 1: 11, Phase 2: +12


@pytest.mark.parametrize(
    ("name", "path", "method", "allowed"), DISALLOWED, ids=[f"{n}-{m}" for n, _, m, _ in DISALLOWED]
)
def test_unsupported_method_returns_405_envelope(world, name, path, method, allowed):
    r = world.generic(method, path, "{}", content_type="application/json")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert r.json()["error"]["rule_id"] is None
    advertised = {m.strip() for m in r["Allow"].split(",")}
    assert allowed <= advertised and method not in advertised


@pytest.mark.parametrize(("name", "path", "allowed"), ROUTES, ids=[r[0] for r in ROUTES])
def test_allowed_methods_are_not_405_and_options_works(world, name, path, allowed):
    for m in allowed:
        assert world.generic(m, path, "{}", content_type="application/json").status_code != 405
    assert world.options(path).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/students/stu-000001/",  # lowercase ID
        "/api/v1/students/STU_000001/",  # wrong separator
        "/api/v1/programs/prg-001/",
        "/api/v1/terms/term-2026-fall/",
        "/api/v1/enrollments/ENR 2026/",
        f"/api/v1/fee-schedules/{FEE}/",  # D14: no fee-schedule detail route
        "/api/v1/fee-schedules/fee-prg-001/history/",
        "/api/v1/invoices/inv-stu-000001/",
        "/api/v1/invoices/INV 1/",
        "/api/v1/students/STU-999999/ledger/",
        "/api/v1/billing/",
        "/api/v1/billing/late-fees/",
        "/api/v1/does-not-exist/",
        "/api/v2/students/",
    ],
)
def test_unknown_or_malformed_api_urls_return_404_envelope(world, path):
    r = world.get(path)
    assert r.status_code == 404
    assert r["Content-Type"].startswith("application/json")
    assert r.json()["error"]["code"] == "NOT_FOUND"
