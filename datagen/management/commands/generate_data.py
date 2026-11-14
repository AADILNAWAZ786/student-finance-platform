"""``python manage.py generate_data``: the synthetic data pipeline (ARCHITECTURE §11.1–§11.2).

Generates the dataset into the configured, fresh ``default`` and ``ground_truth`` databases
(decision D-G6: refuses otherwise, no force option), then runs the blind reconciliation
(stage 18, ``triggered_by = DATAGEN``) and the evaluation (stage 19). Same ``--seed`` + same
scale + same config → the same data, labels and checksums. Phase 7 verifies the starter
scale; full-scale volumes and timing are verified in Phase 8.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from datagen import services
from datagen.config import DEFAULT_SEED, ConfigError
from datagen.pipeline.engine import GenerationError


class Command(BaseCommand):
    help = "Generate the synthetic dataset and its ground truth into fresh databases."

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        scale = parser.add_mutually_exclusive_group()
        scale.add_argument("--scale", choices=("starter", "full"), default=None)
        scale.add_argument("--scale-factor", default=None, help="e.g. 0.05 (0 < f ≤ 1)")
        parser.add_argument(
            "--skip-reconciliation",
            action="store_true",
            help="Stop after writing the data (no reconciliation run, no evaluation).",
        )

    def handle(self, *args, seed, scale, scale_factor, skip_reconciliation, **options):
        if scale is None and scale_factor is None:
            scale = "starter"
        try:
            result = services.generate(
                seed=seed,
                scale=scale,
                scale_factor=scale_factor,
                reconcile=not skip_reconciliation,
            )
        except services.NotFreshError as exc:
            raise CommandError(exc.message) from exc
        except (ConfigError, GenerationError) as exc:
            raise CommandError(str(exc)) from exc
        run = result.run
        self.stdout.write(
            f"{run.run_id} seed={run.seed} scale={run.scale_name} ({run.scale_factor}) "
            f"config_hash={run.config_hash[:16]}… dataset={run.checksums['dataset']}"
        )
        self.stdout.write(
            f"labels={len(result.plan.labels)} labels_checksum={result.plan.labels_checksum}"
        )
        for table, n in sorted(run.row_counts.items()):
            self.stdout.write(f"  {table}: {n}")
        recon = result.reconciliation_run
        if recon is not None:
            s = recon.summary
            self.stdout.write(f"{recon.run_id} {recon.status} findings={s.get('findings_total')}")
            if recon.status != "COMPLETED":
                raise CommandError(f"{recon.run_id} FAILED: {json.dumps(s.get('error'))}")
        ev = result.evaluation
        if ev is not None:
            for issue_type, m in ev["by_type"].items():
                self.stdout.write(
                    f"  {issue_type}: TP={m['tp']} FP={m['fp']} FN={m['fn']} "
                    f"P={m['precision']} R={m['recall']} F1={m['f1']}"
                )
            self.stdout.write(
                f"consequential={ev['consequential']} unexpected={ev['unexpected']} "
                f"non_anomalous_reconciled={ev['students']['pct_non_anomalous_reconciled']}% "
                f"meets_targets={ev['meets_targets']}"
            )
