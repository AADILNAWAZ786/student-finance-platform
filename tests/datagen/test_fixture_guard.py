"""The datagen module fixtures unblock the database themselves; they must refuse to flush or
generate into anything but the configured test databases (Phase 8 safety guard)."""

import pytest
from django.db import connections

from tests.datagen.conftest import assert_test_databases

pytestmark = pytest.mark.django_db(databases=["default", "ground_truth"])


def test_the_test_databases_are_accepted():
    assert_test_databases()


@pytest.mark.parametrize("alias", ["default", "ground_truth"])
def test_a_configured_database_path_is_refused(alias, monkeypatch):
    settings = connections[alias].settings_dict
    owner = str(settings["TEST"]["NAME"]).replace("test_", "")  # e.g. <repo>/db.sqlite3
    monkeypatch.setitem(settings, "NAME", owner)
    with pytest.raises(pytest.fail.Exception, match=f"refusing to flush or generate into {alias}"):
        assert_test_databases()
