"""export_data refusals that need an empty database (Phase 8, P8-D3). A module of its own: the
transactional test flushes the databases, which would wipe the module-scoped starter world."""

import pytest
from django.core.management import call_command

from datagen import export as ex


@pytest.mark.django_db(transaction=True, databases=["default", "ground_truth"])
def test_refuses_without_a_generation_run_or_a_reconciliation_run(tmp_path):
    with pytest.raises(ex.ExportError, match="no generation run in the database"):
        ex.export(tmp_path / "a")
    call_command("generate_data", "--skip-reconciliation")
    with pytest.raises(
        ex.ExportError, match="run `manage.py run_reconciliation --as-of 2026-12-31`"
    ):
        ex.export(tmp_path / "b")
