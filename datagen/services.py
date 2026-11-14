"""Data generation service (ARCHITECTURE §4 datagen: ``generate``, ``inject_anomalies``).

``plan`` builds a whole dataset in memory (stages 1–17) and touches no database: same seed +
same resolved config + same code → the same rows, labels and checksums, on every supported
Python version and whatever ``PYTHONHASHSEED`` is (approved decision D-G2).

``generate`` writes it (decisions D-G5, D-G6):
  * refuses unless both databases are fresh (no domain row, no run, no label) — no force option;
  * writes every domain row and the GenerationRun in one ``default`` transaction, and the labels
    in a ``ground_truth`` transaction nested inside it, so the labels commit **first**:
      - a failure before or while writing the labels rolls both back (nothing is written);
      - a failure of the domain commit after the labels committed leaves labels only. The next
        run refuses (the ground-truth database is not fresh). Recovery: restore both database
        backups taken before generating (or recreate both empty databases), which brings both
        back to one consistent state; see KNOWN_LIMITATIONS P7-O3;
  * then runs the blind reconciliation (stage 18) and the evaluation (stage 19).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from unittest import mock

from django.db import transaction
from django.utils import timezone

from core.exceptions import StateConflict
from core.ids import ground_truth_label_id, next_id
from core.models import IdSequence
from datagen import digest
from datagen.config import DEFAULT_SEED, Settings, resolve
from datagen.models import GenerationRun, GroundTruthLabel
from datagen.pipeline.engine import GenerationError, World, ref_id
from datagen.pipeline.injector import Injector, LabelSpec
from datagen.pipeline.materialize import MODELS, Materialized, build
from datagen.pipeline.scenario import Scenario
from datagen.pipeline.validation import validate
from reconciliation.models import BalanceSnapshot, ReconciliationRun

GT_DB = "ground_truth"
AS_OF = date(2026, 12, 31)


class NotFreshError(StateConflict):
    """generate refused: a database already holds data (decision D-G6; 409 DATASET_NOT_FRESH)."""

    def __init__(self, tables: list[str]):
        super().__init__(
            "DATASET_NOT_FRESH",
            "Generation needs fresh, migrated databases; these tables already hold rows: "
            + ", ".join(tables)
            + ".",
            details={"non_empty_tables": tables},
        )
        self.tables = tables


@dataclass
class Plan:
    settings: Settings
    world: World
    materialized: Materialized
    labels: list[GroundTruthLabel]
    validation: dict

    @property
    def row_counts(self) -> dict:
        return {m._meta.db_table: len(rows) for m, rows in self.materialized.rows.items()}

    @property
    def checksums(self) -> dict:
        tables = digest.instance_digests(self.materialized.rows)
        return {"tables": tables, "dataset": digest.combined(tables)}

    @property
    def labels_checksum(self) -> str:
        return label_checksum(self.labels)


def label_checksum(labels) -> str:
    return digest.table_digest(GroundTruthLabel, labels)


def plan(
    *,
    seed: int = DEFAULT_SEED,
    scale: str | None = "starter",
    scale_factor=None,
    inject: bool = True,
    run_id: str = "GEN-2026-0001",
) -> Plan:
    """Stages 1–17 in memory. ``inject=False`` builds the baseline only (tests)."""
    settings = Settings(seed=int(seed), resolved=resolve(scale=scale, scale_factor=scale_factor))
    scenario = Scenario(settings)
    world = scenario.run()  # stages 1–13
    report = validate(world, scenario.stats, settings.counts, settings.dec("scale_factor"))
    if not report["passed"]:
        raise GenerationError("validation failed: " + "; ".join(report["problems"]))
    injector = Injector(
        world,
        seed=settings.seed,
        per_type=settings.counts["anomalies_per_type"],
        conf=settings.get("anomalies"),
    )
    if inject:
        injector.transactional()  # stage 14
    for d in settings.get("snapshots"):  # stage 15: after the transactional injection
        world.take_snapshots(as_of_date=date.fromisoformat(d))
    if inject:
        injector.balance_mismatches()  # stage 16
    materialized = build(world)  # stage 17
    labels = _labels(injector.labels, run_id)
    return Plan(settings, world, materialized, labels, report)


def _labels(specs: list[LabelSpec], run_id: str) -> list[GroundTruthLabel]:
    order = sorted(specs, key=lambda s: (s.issue_type, s.record.pk))
    out = []
    for n, s in enumerate(order, 1):  # numbered in memory, never from IdSequence (D-G3)
        out.append(
            GroundTruthLabel(
                label_id=ground_truth_label_id(int(run_id[4:8]), n),
                generation_run_id=run_id,
                issue_type=s.issue_type,
                entity_type=s.entity_type,
                record_id=s.record.pk,
                student_id=s.student.pk,
                related_ids=[ref_id(r) for r in s.related],
                rule_violated=s.rule_violated,
                injection_method=s.method,
                expected_value=s.expected_value,
                injected_value=s.injected_value,
                expected_decision=s.expected_decision,
            )
        )
    return out


# --- freshness (D-G6) -----------------------------------------------------------------------------

# findings need a run (FK), so an empty run table means no findings either; datagen.evaluation
# stays the only datagen module that reads findings (ARCHITECTURE §12.2.4)
FRESH_MODELS = (*MODELS, GenerationRun, ReconciliationRun, BalanceSnapshot)


def nonempty_tables() -> list[str]:
    found = sorted({m._meta.db_table for m in FRESH_MODELS if m.objects.using("default").exists()})
    if GroundTruthLabel.objects.using(GT_DB).exists():
        found.append(f"{GT_DB}:{GroundTruthLabel._meta.db_table}")
    return found


def ensure_fresh() -> None:
    tables = nonempty_tables()
    if tables:
        raise NotFreshError(tables)


# --- writing (D-G5) -------------------------------------------------------------------------------


def _after_labels_committed():
    """Test seam: runs after the ground-truth transaction committed, before the domain commit."""


@dataclass
class GenerationResult:
    run: GenerationRun
    plan: Plan
    reconciliation_run: ReconciliationRun | None
    evaluation: dict | None


def generate(
    *,
    seed: int = DEFAULT_SEED,
    scale: str | None = "starter",
    scale_factor=None,
    reconcile: bool = True,
    inject: bool = True,
    code_version: str | None = None,
) -> GenerationResult:
    ensure_fresh()
    started = timezone.now()
    run_id = "GEN-2026-0001"  # the first run of a fresh database (D-G6); confirmed below
    p = plan(seed=seed, scale=scale, scale_factor=scale_factor, run_id=run_id, inject=inject)
    with transaction.atomic(using="default"):
        assigned = next_id("generation_run", year=AS_OF.year)
        if assigned != run_id:  # unreachable on a fresh database
            raise GenerationError(f"unexpected generation run id {assigned}")
        with _frozen_now(started):  # one recorded_at for every row: no ordering leak
            for model, rows in p.materialized.rows.items():
                if model is IdSequence:
                    continue
                model.objects.using("default").bulk_create(rows, batch_size=2000)
            _merge_sequences(p.materialized.sequences)
        run = GenerationRun.objects.using("default").create(
            run_id=run_id,
            seed=p.settings.seed,
            scale_name=p.settings.get("scale_name"),
            scale_factor=p.settings.dec("scale_factor"),
            config_hash=p.settings.hash,
            row_counts=p.row_counts,
            checksums=p.checksums,
            code_version=code_version,
            started_at=started,
            finished_at=timezone.now(),
        )
        with transaction.atomic(using=GT_DB), _frozen_now(started):  # commits first (D-G5)
            GroundTruthLabel.objects.using(GT_DB).bulk_create(p.labels)
        _after_labels_committed()
    recon = evaluation = None
    if reconcile:
        from datagen.evaluation import evaluate
        from reconciliation import services as reconciliation

        recon = reconciliation.run_reconciliation(
            scope="ALL", as_of_date=AS_OF, triggered_by="DATAGEN"
        )  # stage 18
        if recon.status == "COMPLETED":
            evaluation = evaluate(generation_run_id=run.run_id, reconciliation_run_id=recon.pk)
    return GenerationResult(run, p, recon, evaluation)


def _merge_sequences(sequences: dict) -> None:
    """IdSequence rows for every scope used; GEN:2026 already exists from next_id above."""
    existing = set(IdSequence.objects.using("default").values_list("key", flat=True))
    IdSequence.objects.using("default").bulk_create(
        [
            IdSequence(key=k, last_value=v)
            for k, v in sorted(sequences.items())
            if k not in existing
        ],
        batch_size=2000,
    )


class _frozen_now:
    def __init__(self, at: datetime):
        self._patch = mock.patch("django.utils.timezone.now", return_value=at)

    def __enter__(self):
        self._patch.start()

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def database_checksums() -> dict:
    """Checksums of the rows actually stored (compare with GenerationRun.checksums)."""
    models_ = [m for m in MODELS if m is not IdSequence]
    tables = digest.database_digests(models_)
    tables[IdSequence._meta.db_table] = digest.table_digest(
        IdSequence,
        IdSequence.objects.exclude(key__startswith="GEN:")
        .exclude(key__startswith="RECON:")
        .exclude(key__startswith="FND:")
        .order_by("pk"),
    )
    return {"tables": tables, "dataset": digest.combined(tables)}


def stored_label_checksum(run_id: str) -> str:
    return label_checksum(GroundTruthLabel.objects.using(GT_DB).filter(generation_run_id=run_id))
