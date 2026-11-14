"""Run the complete student-finance workflow end to end, in isolated temporary databases.

    python manage.py demo_workflow                         # starter scale, output/demo
    python manage.py demo_workflow --out output/demo-2 --seed 20261022 --keep
    python manage.py demo_workflow --scale full --out output/demo-full   # ~8 min, ~4.4 GB

The configured databases are never opened: the command writes a settings module for two new
SQLite files inside <out>/run/, checks it in a subprocess, and runs every stage there (generate,
reconcile, export, verify_export, the ARCHITECTURE §9.3 API scenario with its exception paths,
the anomalies with their labels, the evaluation). A non-empty --out is refused. Results:
<out>/demo_report.md, <out>/api_transcript.json, <out>/demo_results.json, <out>/export/.
Exits non-zero when any stage fails.
"""

import argparse
import sys

from django.core.management.base import BaseCommand, CommandError

from datagen.demo import DemoError, run_child, run_demo
from datagen.services import DEFAULT_SEED


class Command(BaseCommand):
    help = "Run the end-to-end demo workflow in isolated temporary databases."
    requires_system_checks = []  # the parent must not open the configured databases
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
        scale = parser.add_mutually_exclusive_group()
        scale.add_argument("--scale", choices=("starter", "full"), default=None)
        scale.add_argument("--scale-factor", default=None, help="e.g. 0.05 (0 < f ≤ 1)")
        parser.add_argument("--config", default=None, help="generation config YAML")
        parser.add_argument(
            "--out", default="output/demo", help="new or empty directory (default output/demo)"
        )
        parser.add_argument(
            "--keep", action="store_true", help="keep the demo databases in <out>/run/db/"
        )
        parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)

    def handle(self, *args, seed, scale, scale_factor, config, out, keep, child, **options):
        if child:  # inside the isolated subprocess (refused anywhere else)
            try:
                run_child(
                    out=out,
                    seed=seed,
                    scale=scale,
                    scale_factor=scale_factor,
                    config=config,
                    log=self.stdout,
                )
            except DemoError as exc:
                raise CommandError(str(exc)) from exc
            return
        self.stdout.write(f"demo_workflow: seed {seed}, output {out} (isolated databases)")
        try:
            res = run_demo(
                out=out,
                seed=seed,
                scale=scale,
                scale_factor=scale_factor,
                config=config,
                keep=keep,
                stream=self.stdout,
            )
        except DemoError as exc:
            raise CommandError(str(exc)) from exc
        ev = res["D_evaluation"]
        self.stdout.write("")
        for issue, v in ev["by_type"].items():
            self.stdout.write(
                f"  {issue}: TP={v['tp']} FP={v['fp']} FN={v['fn']} "
                f"P={v['precision']} R={v['recall']} F1={v['f1']}"
            )
        self.stdout.write(
            f"  {res['api_calls']} API calls ({res['api_refusals']} expected refusals); "
            f"export combined_sha256={res['B_export']['combined_sha256']}"
        )
        self.stdout.write(
            f"  runtime {res['runtime_seconds']} s, peak memory {res['peak_rss_mb']} MB"
        )
        self.stdout.write(
            self.style.SUCCESS(f"demo_workflow PASSED: every stage; see {out}/demo_report.md")
        )
        self.stdout.write(f"  transcript: {out}/api_transcript.json   export: {out}/export/")
        if keep:
            self.stdout.write(
                "  browse the demo data: "
                f"PYTHONPATH={out}/run/settings DJANGO_SETTINGS_MODULE=sf_demo_settings "
                f"{sys.executable} manage.py runserver"
            )
