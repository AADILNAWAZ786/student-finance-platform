"""Approved decisions D-G5 (labels commit first, then the domain database) and D-G6 (generation
refuses unless both databases are fresh; no force option).

Failure directions:
  1. the label write fails → both transactions roll back: nothing in either database;
  2. the domain commit fails after the labels committed → labels only. The next generation is
     refused (the ground-truth database is not fresh). Recovery: restore both databases from
     the backups taken before generating (here: the label table emptied, as a restored backup
     would be) — generation then succeeds and both databases are consistent again.
"""

import pytest

from datagen import services
from datagen.models import GenerationRun, GroundTruthLabel
from students.models import Student

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "ground_truth"])

GT = GroundTruthLabel.objects.using("ground_truth")


def _empty() -> bool:
    return services.nonempty_tables() == []


def test_a_label_write_failure_leaves_both_databases_untouched(monkeypatch):
    from django.db.models.query import QuerySet

    original = QuerySet.bulk_create
    written = []

    def labels_fail(self, objs, *args, **kwargs):
        if self.model is GroundTruthLabel:
            # every domain row is already written (uncommitted) when the labels fail
            written.append(Student.objects.count())
            raise RuntimeError("label write failed")
        return original(self, objs, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "bulk_create", labels_fail)
    with pytest.raises(RuntimeError, match="label write failed"):
        services.generate(seed=20261022, scale="starter", reconcile=False)
    monkeypatch.undo()
    assert written == [100]
    assert _empty()
    assert not Student.objects.exists() and not GenerationRun.objects.exists()


def test_a_domain_commit_failure_after_the_labels_leaves_labels_only_and_is_recoverable(
    monkeypatch,
):
    def boom():
        raise RuntimeError("domain commit failed")

    monkeypatch.setattr(services, "_after_labels_committed", boom)
    with pytest.raises(RuntimeError, match="domain commit failed"):
        services.generate(seed=20261022, scale="starter", reconcile=False)
    monkeypatch.undo()
    assert GT.count() == 15  # committed first (D-G5)
    assert not Student.objects.exists() and not GenerationRun.objects.exists()
    with pytest.raises(services.NotFreshError) as refused:  # safe failure: no silent mix
        services.generate(seed=20261022, scale="starter", reconcile=False)
    assert refused.value.tables == ["ground_truth:datagen_groundtruthlabel"]
    GT.all().delete()  # recovery: both databases restored to their pre-generation backups
    result = services.generate(seed=20261022, scale="starter", reconcile=False)
    assert GT.count() == 15
    assert set(GT.values_list("generation_run_id", flat=True)) == {result.run.run_id}


def test_generation_refuses_a_database_with_any_domain_row(student):
    with pytest.raises(services.NotFreshError) as refused:
        services.generate(seed=20261022, scale="starter")
    e = refused.value
    assert (e.code, e.status_code) == ("DATASET_NOT_FRESH", 409)
    assert "students_student" in e.tables
    assert GT.count() == 0


def test_generation_refuses_a_second_run_without_any_force_option():
    services.generate(seed=20261022, scale="starter", reconcile=False)
    import inspect

    assert "force" not in inspect.signature(services.generate).parameters
    with pytest.raises(services.NotFreshError) as refused:
        services.generate(seed=20261022, scale="starter", reconcile=False)
    assert "datagen_generationrun" in refused.value.tables
    assert "ground_truth:datagen_groundtruthlabel" in refused.value.tables
    assert GenerationRun.objects.count() == 1 and GT.count() == 15
