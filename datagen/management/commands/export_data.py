"""``python manage.py export_data --out output/starter``: the deterministic dataset export
(ARCHITECTURE §11.1, §11.4; Phase 8). See ``datagen.export`` for the layout and the checks
that run before anything is written (one unmodified generated dataset, config and code that
still reproduce it, an output directory that is new or empty).
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from datagen.config import ConfigError
from datagen.export import ExportError, export
from datagen.models import GenerationRun
from datagen.pipeline.engine import GenerationError


class Command(BaseCommand):
    help = "Export the generated dataset, ground truth, reports and manifests."

    def add_arguments(self, parser):
        parser.add_argument("--out", help="Output directory (new or empty; default output/<scale>)")
        parser.add_argument("--config", default=None, help="The generation config that was used")
        parser.add_argument("--reconciliation-run", default=None, help="RECON-… (default: DATAGEN)")

    def handle(self, *args, out, config, reconciliation_run, **options):
        if out is None:
            run = GenerationRun.objects.order_by("run_id").first()
            out = Path(settings.BASE_DIR) / "output" / (run.scale_name if run else "starter")
        try:
            result = export(out, config_path=config, reconciliation_run=reconciliation_run)
        except (ExportError, ConfigError, GenerationError) as exc:
            raise CommandError(str(exc)) from exc
        m = result.manifest
        self.stdout.write(
            f"exported {m['generation_run']} (seed {m['seed']}, {m['scale_name']}) to {result.out}"
        )
        self.stdout.write(
            f"{len(m['files'])} checksummed files, {sum(m['row_counts'].values())} dataset rows, "
            f"{m['ground_truth_rows']} labels, {m['findings_rows']} findings"
        )
        self.stdout.write(f"combined_sha256={m['combined_sha256']}")
