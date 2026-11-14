"""Check that a seed reproduces byte-identical exports (Phase 8, P8-D4).

    python manage.py verify_reproducibility --seed 20261022 --scale starter
    python manage.py verify_reproducibility --scale full --workdir /tmp/sf-repro-full --keep

Runs migrate + generate_data + export_data twice, one after the other, each in its own
subprocess with its own settings module and fresh SQLite databases inside the work directory
(the configured databases are never opened), then compares every deterministic export file by
sha256. run_log.json is excluded. Writes <workdir>/reproducibility_report.md and exits non-zero
on any difference, naming the first differing file, primary key, column and values.
"""

from django.core.management.base import BaseCommand, CommandError

from datagen.reproducibility import ReproducibilityError, reproduce
from datagen.services import DEFAULT_SEED


class Command(BaseCommand):
    help = "Generate and export one seed twice in isolated databases and compare the files."
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        scale = parser.add_mutually_exclusive_group()
        scale.add_argument("--scale", choices=("starter", "full"), default=None)
        scale.add_argument("--scale-factor", default=None, help="e.g. 0.05 (0 < f ≤ 1)")
        parser.add_argument("--config", default=None, help="generation config YAML")
        parser.add_argument(
            "--workdir", default=None, help="empty or new directory (default: a new temp dir)"
        )
        parser.add_argument(
            "--keep", action="store_true", help="keep both runs' databases and exports"
        )

    def handle(self, *args, seed, scale, scale_factor, config, workdir, keep, **options):
        try:
            outcome = reproduce(
                seed=seed,
                scale=scale,
                scale_factor=scale_factor,
                config=config,
                workdir=workdir,
                keep=keep,
            )
        except ReproducibilityError as exc:
            raise CommandError(str(exc)) from exc
        for run in outcome.runs:
            self.stdout.write(
                f"{run.name}: combined_sha256={run.combined_sha256} files={run.files} "
                f"rows={run.rows} generate={run.steps.get('generate_data')}s "
                f"export={run.steps.get('export_data')}s"
            )
        self.stdout.write(f"report: {outcome.report_path}")
        if not outcome.identical:
            for d in outcome.differences[:10]:
                self.stdout.write(f"  - {d}")
            raise CommandError(
                f"NOT REPRODUCIBLE: {len(outcome.differences)} difference(s) in "
                f"{outcome.compared_files} files; runs kept in {outcome.workdir}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"REPRODUCIBLE: {outcome.compared_files} files byte-identical "
                f"(run_log.json excluded)"
            )
        )
