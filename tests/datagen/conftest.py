"""Phase 7 fixtures: one generated starter world per module, committed to the test databases
and flushed afterwards (generation commits its own transactions, so it cannot run inside the
per-test rollback). Tests read it inside their usual transaction.

Safety (Phase 8): these fixtures unblock database access themselves, so pytest-django's own
guard does not apply. If a module using them carries no ``django_db`` mark for both databases,
the test databases are never created and the connections still point at the configured
(owner's) files; flushing then would destroy them. ``_flush`` therefore refuses unless every
connection points at its configured TEST database."""

import pytest
from django.core.management import call_command
from django.db import connections

from datagen import services

DATABASES = ["default", "ground_truth"]


def assert_test_databases():
    for alias in DATABASES:
        settings = connections[alias].settings_dict
        name, test_name = str(settings["NAME"]), settings.get("TEST", {}).get("NAME")
        in_memory = name == ":memory:" or "mode=memory" in name
        if not (in_memory or (test_name and name == str(test_name))):
            pytest.fail(
                f"refusing to flush or generate into {alias} database {name}: it is not the "
                f"test database {test_name}. Mark the module with "
                'pytest.mark.django_db(databases=["default", "ground_truth"]).',
                pytrace=False,
            )


def _flush():
    assert_test_databases()
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
