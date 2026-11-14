"""``python manage.py evaluate``: compare a reconciliation run with the ground truth
(ARCHITECTURE §11.1, §12.2.4–§12.3) and write ``evaluation_report.json``.

The report is the only thing evaluation writes. Default output path:
``output/<scale>/reports/evaluation_report.json`` (ARCHITECTURE §11.4 layout).
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from datagen.evaluation import EvaluationError, evaluate
from datagen.models import GenerationRun
from reconciliation.models import ReconciliationRun


class Command(BaseCommand):
    help = "Evaluate a reconciliation run against the ground-truth labels."

    def add_arguments(self, parser):
        parser.add_argument("--generation-run", help="GEN-… (default: the only/latest run)")
        parser.add_argument(
            "--reconciliation-run", help="RECON-… (default: the latest COMPLETED ALL-scope run)"
        )
        parser.add_argument("--out", help="Report path (default output/<scale>/reports/…)")

    def handle(self, *args, generation_run, reconciliation_run, out, **options):
        gen = (
            GenerationRun.objects.filter(pk=generation_run).first()
            if generation_run
            else GenerationRun.objects.order_by("-run_id").first()
        )
        if gen is None:
            raise CommandError("No generation run found; run generate_data first.")
        if reconciliation_run is None:
            latest = (
                ReconciliationRun.objects.filter(status="COMPLETED", scope="ALL")
                .order_by("-run_id")
                .first()
            )
            if latest is None:
                raise CommandError("No COMPLETED ALL-scope reconciliation run found.")
            reconciliation_run = latest.pk
        try:
            report = evaluate(generation_run_id=gen.pk, reconciliation_run_id=reconciliation_run)
        except EvaluationError as exc:
            raise CommandError(str(exc)) from exc
        path = (
            Path(out)
            if out
            else (
                Path(settings.BASE_DIR)
                / "output"
                / gen.scale_name
                / "reports"
                / "evaluation_report.json"
            )
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for issue_type, m in report["by_type"].items():
            self.stdout.write(
                f"{issue_type}: TP={m['tp']} FP={m['fp']} FN={m['fn']} "
                f"P={m['precision']} R={m['recall']} F1={m['f1']}"
            )
        self.stdout.write(
            f"consequential={report['consequential']} unexpected={report['unexpected']} "
            f"meets_targets={report['meets_targets']}"
        )
        self.stdout.write(f"wrote {path}")
