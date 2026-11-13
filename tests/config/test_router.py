"""GroundTruthRouter (ARCHITECTURE.md §12.2): labels only in ground_truth, nothing else there."""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.db import connections

from config.routers import GroundTruthRouter
from core.models import IdSequence

pytestmark = pytest.mark.phase0
router = GroundTruthRouter()


def _model(app_label, model_name):
    return SimpleNamespace(_meta=SimpleNamespace(app_label=app_label, model_name=model_name))


LABEL = _model("datagen", "groundtruthlabel")
RUN = _model("datagen", "generationrun")


def test_ground_truth_label_reads_and_writes_go_to_ground_truth():
    assert router.db_for_read(LABEL) == "ground_truth"
    assert router.db_for_write(LABEL) == "ground_truth"


@pytest.mark.parametrize("model", [User, IdSequence, RUN, _model("payments", "payment")])
def test_everything_else_goes_to_default(model):
    assert router.db_for_read(model) == "default"
    assert router.db_for_write(model) == "default"


def test_migrations_allowed_only_on_the_right_side():
    assert router.allow_migrate("ground_truth", "datagen", "groundtruthlabel") is True
    assert router.allow_migrate("ground_truth", "datagen", "generationrun") is False
    assert router.allow_migrate("ground_truth", "auth", "user") is False
    assert router.allow_migrate("ground_truth", "datagen", None) is False  # RunPython/RunSQL
    assert router.allow_migrate("default", "datagen", "groundtruthlabel") is False
    assert router.allow_migrate("default", "payments", "payment") is True
    assert router.allow_migrate("default", "datagen", None) is True
    assert router.allow_migrate("some_other_db", "payments", "payment") is False


def test_no_relations_across_databases():
    assert router.allow_relation(LABEL, User) is False
    assert router.allow_relation(User, LABEL) is False
    assert router.allow_relation(LABEL, LABEL) is True
    assert router.allow_relation(User, IdSequence) is True


@pytest.mark.django_db(databases=["default", "ground_truth"])
def test_migrated_test_databases_are_isolated():
    default_tables = set(connections["default"].introspection.table_names())
    gt_tables = set(connections["ground_truth"].introspection.table_names())
    assert {"auth_user", "core_idsequence", "authtoken_token"} <= default_tables
    assert gt_tables == {"django_migrations"}  # GroundTruthLabel arrives in Phase 7
