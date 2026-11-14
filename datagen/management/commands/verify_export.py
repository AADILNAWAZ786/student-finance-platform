"""Validate an exported dataset directory (Phase 8, P8-D5).

    python manage.py verify_export output/starter

Reads only the files: re-hashes them against dataset_manifest.json, compares CSV, JSON and SQL
row by row (the SQL is loaded into a temporary SQLite file, never a Django database), and checks
IDs, foreign keys, the ground-truth labels and their separation from dataset/. Exits non-zero
when any check fails. There is no import: this command never writes to a database.
"""

from django.core.management.base import BaseCommand, CommandError

from datagen.verification import VerificationError, verify


class Command(BaseCommand):
    help = "Validate an export_data directory (hashes, CSV/JSON/SQL, IDs, FKs, labels)."
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("path", help="the directory export_data wrote")

    def handle(self, *args, path, **options):
        try:
            report = verify(path)
        except VerificationError as exc:
            raise CommandError(str(exc)) from exc
        for line in report.lines():
            self.stdout.write(line)
        summary = f"{report.tables} tables, {report.rows} dataset rows in {report.out}"
        if not report.ok:
            failed = [c.name for c in report.checks.values() if not c.ok]
            raise CommandError(f"export verification FAILED ({', '.join(failed)}): {summary}")
        self.stdout.write(self.style.SUCCESS(f"export verification PASSED: {summary}"))
