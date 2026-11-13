"""Django Admin for payments (corrective Phase 4 delta): the four models are registered and
read-only. Pages load, but nothing can be added, changed or deleted, so no record can bypass
payments.services (IDs, ledger entries, caps, events)."""

from datetime import date
from decimal import Decimal as D

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import Client

from payments import services
from payments.models import Payment, PaymentAllocation, Refund, RefundEvent

pytestmark = pytest.mark.django_db

MODELS = [Payment, PaymentAllocation, Refund, RefundEvent]


@pytest.fixture
def site_client(db):
    c = Client()
    c.force_login(User.objects.create_superuser("root"))
    return c


@pytest.fixture
def records(record):
    payment = record("174000.00", payer="SPONSOR", auto=True)
    refund = services.request_refund(
        payment=payment, amount=D("100.00"), requested_date=date(2026, 8, 21), actor="t"
    )
    return payment, refund


def test_the_four_models_are_registered_read_only(rf):
    request = rf.get("/")
    request.user = User(is_superuser=True, is_staff=True)
    for model in MODELS:
        model_admin = admin.site._registry[model]
        assert model_admin.has_add_permission(request) is False, model
        assert model_admin.has_change_permission(request) is False, model
        assert model_admin.has_delete_permission(request) is False, model


def test_pages_load_and_are_view_only(site_client, records):
    payment, refund = records
    alloc = payment.allocations.first()
    event = refund.events.get()
    pages = {
        "payment": payment.pk,
        "paymentallocation": alloc.pk,
        "refund": refund.pk,
        "refundevent": event.pk,
    }
    for name, pk in pages.items():
        assert site_client.get(f"/admin/payments/{name}/").status_code == 200, name
        page = site_client.get(f"/admin/payments/{name}/{pk}/change/")
        assert page.status_code == 200, name
        assert b'name="_save"' not in page.content  # no save button: view only
        assert site_client.get(f"/admin/payments/{name}/add/").status_code == 403, name
        assert site_client.get(f"/admin/payments/{name}/{pk}/delete/").status_code == 403, name


def test_posting_a_change_through_admin_is_refused(site_client, records):
    payment, refund = records
    before = Refund.objects.filter(pk=refund.pk).values().get()
    r = site_client.post(
        f"/admin/payments/refund/{refund.pk}/change/", {"amount": "1.00", "status": "PROCESSED"}
    )
    assert r.status_code == 403
    r = site_client.post(f"/admin/payments/payment/{payment.pk}/change/", {"amount": "1.00"})
    assert r.status_code == 403
    assert Refund.objects.filter(pk=refund.pk).values().get() == before
    assert Payment.objects.get(pk=payment.pk).amount == D("174000.00")
