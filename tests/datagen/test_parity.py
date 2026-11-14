"""ARCHITECTURE §5 parity (approved decision D-G1): the in-memory generator and the real Phase 1–6
services produce the same rows. Every baseline operation the generator performed is replayed
through the service it mirrors, in the same order; all 23 domain tables (IdSequence included)
must then be identical to the generator's stage-17 output, row for row, IDs included.

This replays the whole starter baseline (a superset of the "sample" the design asks for).
"""

import pytest
from django.db import transaction

from datagen import digest, services
from datagen.pipeline.materialize import MODELS
from tests.datagen_replay import replay

pytestmark = pytest.mark.django_db


@pytest.fixture(scope="module")
def planned():
    return services.plan(seed=20261022, scale="starter", inject=False)


def test_every_generator_operation_has_a_service_replay(planned):
    ops = {op for op, _ in planned.world.ops}
    assert ops == {
        "create_program",
        "create_term",
        "create_fee_schedule",
        "new_fee_schedule_version",
        "create_scholarship_type",
        "create_student",
        "change_status",
        "enroll",
        "complete_enrollment",
        "generate_invoice",
        "evaluate_eligibility",
        "award_scholarship",
        "apply_scholarship",
        "record_payment",
        "assess_late_fees",
        "exit_enrollment",
        "request_refund",
        "advance_refund",
        "refresh_collection_status",
        "take_snapshots",
    }


def test_services_reproduce_the_generated_baseline_row_for_row(planned):
    with transaction.atomic():
        replay(planned.world.ops)
    for model in MODELS:
        generated = {i.pk: digest.row(i) for i in planned.materialized.rows[model]}
        replayed = {i.pk: digest.row(i) for i in model.objects.all()}
        assert set(replayed) == set(generated), model.__name__
        diffs = [pk for pk in generated if generated[pk] != replayed[pk]]
        assert diffs == [], (model.__name__, diffs[:3])
    assert digest.instance_digests(planned.materialized.rows) == digest.database_digests(MODELS)
