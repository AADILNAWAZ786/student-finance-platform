"""V-REC: Phase 6 migration safety and isolation.

* reconciliation 0001 (required by DATABASE_DESIGN §3) only creates its three tables and
  their indexes: no other table is touched, no UPDATE or DELETE
* the ground-truth database records the migration but gets no reconciliation table
* migrating reconciliation back to zero and forward again keeps every Phase 1–5 row
* the frozen §12.2 import rule holds at runtime too (see also
  tests/architecture/test_ground_truth_isolation.py and the services test that counts
  label-database queries)
"""

import re

import pytest
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

from tests.verification.test_verify_payments import _sql

RECONCILIATION_TABLES = {
    "reconciliation_balancesnapshot",
    "reconciliation_reconciliationrun",
    "reconciliation_anomalyfinding",
}


@pytest.mark.django_db(transaction=True)
def test_reconciliation_migration_only_creates_its_three_tables():
    statements = _sql("reconciliation", "0001_initial")
    created = {
        re.match(r'CREATE TABLE "(\w+)"', s).group(1)
        for s in statements
        if s.startswith("CREATE TABLE")
    }
    assert created == RECONCILIATION_TABLES
    for sql in statements:
        assert re.match(r'CREATE TABLE "reconciliation_\w+"', sql) or re.match(
            r'CREATE (UNIQUE )?INDEX "\w+" ON "reconciliation_\w+"', sql
        ), sql[:120]
    assert not any(s.upper().startswith(("UPDATE", "DELETE", "INSERT")) for s in statements)


def test_reconciliation_is_the_only_new_migration():
    from django.db.migrations.loader import MigrationLoader

    graph = MigrationLoader(None, ignore_no_migrations=True).graph
    ours = sorted(n for n in graph.leaf_nodes() if n[0] == "reconciliation")
    assert ours == [("reconciliation", "0001_initial")]
    assert graph.node_map[("reconciliation", "0001_initial")].parents == {
        graph.node_map[("students", "0001_initial")]
    }


@pytest.mark.django_db(databases="__all__")
def test_ground_truth_records_but_creates_no_reconciliation_table():
    assert not [
        t
        for t in connections["ground_truth"].introspection.table_names()
        if t.startswith("reconciliation_")
    ]
    applied = MigrationExecutor(connections["ground_truth"]).loader.applied_migrations
    assert ("reconciliation", "0001_initial") in applied
    assert RECONCILIATION_TABLES <= set(connections["default"].introspection.table_names())


def _rows():
    out = {}
    with connection.cursor() as cur:
        for table in sorted(connection.introspection.table_names()):
            if table.startswith("reconciliation_") or table == "django_migrations":
                continue
            cur.execute(f'SELECT * FROM "{table}"')  # noqa: S608 - names from introspection
            out[table] = sorted(map(repr, cur.fetchall()))
    return out


@pytest.mark.django_db(transaction=True)
def test_migrating_back_and_forward_keeps_every_phase5_row(clean_world):
    before = _rows()
    assert sum(len(v) for v in before.values()) > 500  # a populated Phase 5 world
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([("reconciliation", None)])
        assert not set(connection.introspection.table_names()) & RECONCILIATION_TABLES
        assert _rows() == before
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        assert RECONCILIATION_TABLES <= set(connection.introspection.table_names())
        assert _rows() == before
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
