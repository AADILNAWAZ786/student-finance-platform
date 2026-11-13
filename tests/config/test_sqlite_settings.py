"""SQLite configuration and transaction behaviour (ARCHITECTURE.md §8)."""

import sqlite3

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction

from core.checks import check_sqlite_setup

pytestmark = pytest.mark.phase0


def test_both_databases_configured_as_designed():
    for alias in ("default", "ground_truth"):
        cfg = settings.DATABASES[alias]
        assert cfg["ENGINE"] == "django.db.backends.sqlite3"
        assert cfg["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
        assert cfg["OPTIONS"]["timeout"] == 20
    assert settings.DATABASE_ROUTERS == ["config.routers.GroundTruthRouter"]


def test_runtime_versions():
    import django

    assert django.VERSION[:2] == (5, 2)
    assert sqlite3.sqlite_version_info >= (3, 38, 0)


@pytest.mark.django_db
def test_default_db_pragmas_from_init_command():
    with connection.cursor() as cur:
        cur.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0] == "wal"
        cur.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT json_valid('{\"a\": 1}')")
        assert cur.fetchone()[0] == 1


@pytest.mark.django_db(transaction=True)
def test_atomic_takes_the_write_lock_at_begin():
    """IMMEDIATE: entering atomic() locks the database before any write happens.

    With the default DEFERRED mode a second writer could still BEGIN IMMEDIATE here.
    """
    path = settings.DATABASES["default"]["TEST"]["NAME"]
    connection.ensure_connection()
    other = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")  # free outside a Django transaction
        other.execute("ROLLBACK")
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SELECT 1")  # a read only; no write yet
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")  # released again after commit
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_system_check_passes_with_project_settings():
    assert check_sqlite_setup() == []
    call_command("check")  # raises SystemCheckError on any error


def test_system_check_catches_misconfiguration():
    bad = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "OPTIONS": {}},
        "ground_truth": {
            "ENGINE": "django.db.backends.postgresql",
            "OPTIONS": {"transaction_mode": "IMMEDIATE"},
        },
    }
    ids = {e.id for e in check_sqlite_setup(databases=bad, routers=[], sqlite_version=(3, 31, 0))}
    assert ids == {"core.E001", "core.E003", "core.E004", "core.E005"}
    missing = {e.id for e in check_sqlite_setup(databases={}, routers=[])}
    assert "core.E002" in missing
