"""Phase 7 fixtures: one generated starter world per module, committed to the test databases
and flushed afterwards (generation commits its own transactions, so it cannot run inside the
per-test rollback). Tests read it inside their usual transaction."""

import pytest
from django.core.management import call_command

from datagen import services

DATABASES = ["default", "ground_truth"]


def _flush():
    for alias in DATABASES:
        call_command("flush", database=alias, interactive=False, verbosity=0)


@pytest.fixture(scope="module")
def generated(django_db_setup, django_db_blocker):
    """The starter dataset with anomalies, its reconciliation run and evaluation."""
    with django_db_blocker.unblock():
        _flush()
        result = services.generate(seed=20261022, scale="starter")
        yield result
        _flush()


@pytest.fixture(scope="module")
def baseline(django_db_setup, django_db_blocker):
    """The starter baseline only (no injection), reconciled blind."""
    with django_db_blocker.unblock():
        _flush()
        result = services.generate(seed=20261022, scale="starter", inject=False)
        yield result
        _flush()
